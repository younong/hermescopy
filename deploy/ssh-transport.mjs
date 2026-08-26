import { timingSafeEqual } from "node:crypto";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";
import { Client } from "ssh2";

import { redactDeploySecret, runLocalText } from "./local-platform.mjs";

const MAX_OUTPUT_BYTES = 64 * 1024 * 1024;
const CONNECT_TIMEOUT_MS = 15_000;
const OPERATION_TIMEOUT_MS = 60 * 60 * 1000;

function lookupHost(host, port) {
  return String(port) === "22" ? host : `[${host}]:${port}`;
}

function knownHostsPath() {
  return path.join(homedir(), ".ssh", "known_hosts");
}

export function loadKnownHostKeys(host, port, { filePath = knownHostsPath() } = {}) {
  if (!existsSync(filePath)) {
    throw new Error(
      `SSH host key is not trusted yet. Verify it out of band and add ${lookupHost(host, port)} to ${filePath}.`,
    );
  }
  let output;
  try {
    output = runLocalText("ssh-keygen", ["-F", lookupHost(host, port), "-f", filePath]);
  } catch {
    throw new Error(
      `SSH host key is not trusted yet. Verify it out of band and add ${lookupHost(host, port)} to ${filePath}.`,
    );
  }

  const keys = [];
  for (const line of output.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const fields = trimmed.split(/\s+/);
    const offset = fields[0].startsWith("@") ? 1 : 0;
    if (offset !== 0) {
      if (fields[0] === "@revoked") {
        throw new Error(`SSH host key for ${lookupHost(host, port)} is revoked.`);
      }
      continue;
    }
    if (fields.length < 3) continue;
    try {
      keys.push(Buffer.from(fields[2], "base64"));
    } catch {
      // Ignore malformed entries and fail closed below if nothing remains.
    }
  }
  if (!keys.length) {
    throw new Error(`No supported trusted SSH host key found for ${lookupHost(host, port)}.`);
  }
  return keys;
}

export function createKnownHostsVerifier(host, port, options = {}) {
  const trustedKeys = loadKnownHostKeys(host, port, options);
  return (receivedKey) => trustedKeys.some(
    (trustedKey) => trustedKey.length === receivedKey.length && timingSafeEqual(trustedKey, receivedKey),
  );
}

function connectionConfig(args, password, options = {}) {
  return {
    host: args.host,
    port: Number(args.port),
    username: args.user,
    password,
    readyTimeout: CONNECT_TIMEOUT_MS,
    keepaliveInterval: 15_000,
    keepaliveCountMax: 3,
    hostVerifier: createKnownHostsVerifier(args.host, args.port, options),
  };
}

function withTimeout(promise, label, timeoutMs = OPERATION_TIMEOUT_MS) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`${label} timed out.`)), timeoutMs);
    }),
  ]).finally(() => clearTimeout(timer));
}

function connectClient(config) {
  return new Promise((resolve, reject) => {
    const client = new Client();
    client.once("ready", () => resolve(client));
    client.once("error", (error) => reject(new Error(redactDeploySecret(error.message), { cause: error })));
    client.connect(config);
  });
}

export function quoteRemoteArg(value) {
  const text = String(value);
  return `'${text.replaceAll("'", `'"'"'`)}'`;
}

// sshd joins command argv with spaces before handing it to the remote shell,
// so every transport must send a single pre-quoted command string.
export function remoteCommand(command, commandArgs) {
  return [command, ...commandArgs].map(quoteRemoteArg).join(" ");
}

function execOnClient(client, command, commandArgs, { input } = {}) {
  return new Promise((resolve, reject) => {
    client.exec(remoteCommand(command, commandArgs), (error, channel) => {
      if (error) {
        reject(error);
        return;
      }
      const stdout = [];
      const stderr = [];
      let stdoutBytes = 0;
      let stderrBytes = 0;
      let settled = false;

      const fail = (message) => {
        if (settled) return;
        settled = true;
        channel.close();
        reject(new Error(message));
      };
      channel.on("data", (chunk) => {
        stdoutBytes += chunk.length;
        if (stdoutBytes > MAX_OUTPUT_BYTES) {
          fail("Remote command stdout exceeded 64 MiB.");
          return;
        }
        stdout.push(chunk);
      });
      channel.stderr.on("data", (chunk) => {
        stderrBytes += chunk.length;
        if (stderrBytes > MAX_OUTPUT_BYTES) {
          fail("Remote command stderr exceeded 64 MiB.");
          return;
        }
        stderr.push(chunk);
      });
      channel.once("error", (channelError) => fail(redactDeploySecret(channelError.message)));
      channel.once("close", (status, signal) => {
        if (settled) return;
        settled = true;
        const result = {
          stdout: redactDeploySecret(Buffer.concat(stdout).toString("utf8").trim()),
          stderr: redactDeploySecret(Buffer.concat(stderr).toString("utf8").trim()),
          status: typeof status === "number" ? status : 1,
          signal,
        };
        if (result.status !== 0) {
          const failure = new Error(
            `${remoteCommand(command, commandArgs)} failed${result.stderr ? `:\n${result.stderr}` : ""}`,
          );
          failure.commandResult = result;
          reject(failure);
          return;
        }
        resolve(result);
      });
      if (input === undefined) channel.end();
      else channel.end(input);
    });
  });
}

export async function runPasswordSsh(args, remoteArgs, { input, knownHostsFile } = {}) {
  const password = process.env.HERMES_DEPLOY_PASSWORD;
  if (!password) throw new Error("HERMES_DEPLOY_PASSWORD is required for password authentication.");
  const [command, ...commandArgs] = remoteArgs;
  const client = await connectClient(connectionConfig(args, password, { filePath: knownHostsFile }));
  try {
    return await withTimeout(execOnClient(client, command, commandArgs, { input }), "Remote SSH command");
  } finally {
    client.end();
  }
}

function sftpClient(client) {
  return new Promise((resolve, reject) => {
    client.sftp((error, sftp) => error ? reject(error) : resolve(sftp));
  });
}

function fastPut(sftp, localPath, remotePath) {
  return new Promise((resolve, reject) => {
    sftp.fastPut(localPath, remotePath, (error) => error ? reject(error) : resolve());
  });
}

export async function uploadPasswordFile(args, localPath, remotePath, { knownHostsFile } = {}) {
  const password = process.env.HERMES_DEPLOY_PASSWORD;
  if (!password) throw new Error("HERMES_DEPLOY_PASSWORD is required for password authentication.");
  const client = await connectClient(connectionConfig(args, password, { filePath: knownHostsFile }));
  let sftp;
  try {
    sftp = await withTimeout(sftpClient(client), "SFTP session");
    await withTimeout(fastPut(sftp, localPath, remotePath), "SFTP upload");
    return { stdout: "", stderr: "", status: 0 };
  } catch (error) {
    throw new Error(redactDeploySecret(error.message), { cause: error });
  } finally {
    if (sftp) sftp.end();
    client.end();
  }
}
