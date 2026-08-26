import assert from "node:assert/strict";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import ssh2 from "ssh2";
import sftpProtocol from "ssh2/lib/protocol/SFTP.js";

import {
  createKnownHostsVerifier,
  quoteRemoteArg,
  runPasswordSsh,
  uploadPasswordFile,
} from "../../deploy/ssh-transport.mjs";

const { Server, utils } = ssh2;
const { OPEN_MODE, STATUS_CODE } = sftpProtocol;
const keys = utils.generateKeyPairSync("ed25519");
const hostKey = Buffer.from(keys.public.split(/\s+/)[1], "base64");

function fixtureDir() {
  return mkdtempSync(path.join(tmpdir(), "hermes-deploy-ssh-test-"));
}

function knownHostsFile(directory, port, key = keys.public) {
  const fields = key.trim().split(/\s+/);
  const filePath = path.join(directory, "known_hosts");
  writeFileSync(filePath, `[127.0.0.1]:${port} ${fields[0]} ${fields[1]}\n`, "utf8");
  return filePath;
}

function startServer({ uploads } = {}) {
  const server = new Server({ hostKeys: [keys.private] }, (client) => {
    client.on("error", () => {
      // Expected when a client rejects the fixture host key during key exchange.
    });
    client.on("authentication", (context) => {
      if (
        context.method === "password" &&
        context.username === "deploy" &&
        context.password === "fake-sentinel-password"
      ) context.accept();
      else context.reject();
    });
    client.on("ready", () => {
      client.on("session", (accept) => {
        const session = accept();
        session.on("exec", (accept, _reject, info) => {
          const channel = accept();
          const input = [];
          channel.on("data", (chunk) => input.push(chunk));
          channel.on("end", () => {
            channel.write(`command=${info.command}\n`);
            channel.write(`input=${Buffer.concat(input).toString("utf8")}\n`);
            channel.exit(0);
            channel.end();
          });
        });
        session.on("sftp", (accept) => {
          const stream = accept();
          const openFiles = new Map();
          let handleCount = 0;
          stream.on("OPEN", (requestId, filename, flags) => {
            if (!uploads || !(flags & OPEN_MODE.WRITE)) {
              stream.status(requestId, STATUS_CODE.FAILURE);
              return;
            }
            const handle = Buffer.alloc(4);
            handle.writeUInt32BE(handleCount, 0);
            openFiles.set(handleCount, { filename, chunks: [] });
            handleCount += 1;
            stream.handle(requestId, handle);
          });
          stream.on("FSTAT", (requestId, handle) => {
            const file = handle.length === 4 ? openFiles.get(handle.readUInt32BE(0)) : undefined;
            if (!file) stream.status(requestId, STATUS_CODE.FAILURE);
            else stream.attrs(requestId, { size: 0 });
          });
          stream.on("WRITE", (requestId, handle, offset, data) => {
            const file = handle.length === 4 ? openFiles.get(handle.readUInt32BE(0)) : undefined;
            if (!file) {
              stream.status(requestId, STATUS_CODE.FAILURE);
              return;
            }
            file.chunks.push({ offset, data: Buffer.from(data) });
            stream.status(requestId, STATUS_CODE.OK);
          });
          stream.on("CLOSE", (requestId, handle) => {
            const handleId = handle.length === 4 ? handle.readUInt32BE(0) : -1;
            const file = openFiles.get(handleId);
            if (!file) {
              stream.status(requestId, STATUS_CODE.FAILURE);
              return;
            }
            const size = file.chunks.reduce(
              (maximum, chunk) => Math.max(maximum, chunk.offset + chunk.data.length),
              0,
            );
            const contents = Buffer.alloc(size);
            for (const chunk of file.chunks) chunk.data.copy(contents, chunk.offset);
            uploads.set(file.filename, contents);
            openFiles.delete(handleId);
            stream.status(requestId, STATUS_CODE.OK);
          });
        });
      });
    });
  });
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolve(server));
  });
}

function closeServer(server) {
  return new Promise((resolve) => server.close(resolve));
}

test("remote argument quoting preserves apostrophes", () => {
  assert.equal(quoteRemoteArg("a'b"), `'a'"'"'b'`);
});

test("known-host verifier accepts only the pinned key", () => {
  const directory = fixtureDir();
  try {
    const filePath = path.join(directory, "known_hosts");
    writeFileSync(filePath, `example.test ${keys.public.trim()}\n`, "utf8");
    const verifier = createKnownHostsVerifier("example.test", 22, { filePath });
    assert.equal(verifier(hostKey), true);
    assert.equal(verifier(Buffer.alloc(hostKey.length)), false);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test("password transport verifies host key and forwards remote stdin", async () => {
  const directory = fixtureDir();
  const server = await startServer();
  const port = server.address().port;
  const previous = process.env.HERMES_DEPLOY_PASSWORD;
  process.env.HERMES_DEPLOY_PASSWORD = "fake-sentinel-password";
  try {
    const result = await runPasswordSsh(
      { host: "127.0.0.1", port: String(port), user: "deploy" },
      ["bash", "-s", "--", "value with spaces"],
      { input: "printf test\n", knownHostsFile: knownHostsFile(directory, port) },
    );
    assert.match(result.stdout, /command='bash' '-s' '--' 'value with spaces'/);
    assert.match(result.stdout, /input=printf test/);
    assert.doesNotMatch(result.stdout + result.stderr, /fake-sentinel-password/);
  } finally {
    if (previous === undefined) delete process.env.HERMES_DEPLOY_PASSWORD;
    else process.env.HERMES_DEPLOY_PASSWORD = previous;
    await closeServer(server);
    rmSync(directory, { recursive: true, force: true });
  }
});

test("password transport rejects an untrusted host key", async () => {
  const directory = fixtureDir();
  const server = await startServer();
  const port = server.address().port;
  const otherKeys = utils.generateKeyPairSync("ed25519");
  const previous = process.env.HERMES_DEPLOY_PASSWORD;
  process.env.HERMES_DEPLOY_PASSWORD = "fake-sentinel-password";
  try {
    await assert.rejects(
      runPasswordSsh(
        { host: "127.0.0.1", port: String(port), user: "deploy" },
        ["true"],
        { knownHostsFile: knownHostsFile(directory, port, otherKeys.public) },
      ),
      /Host denied|handshake|verification/i,
    );
  } finally {
    if (previous === undefined) delete process.env.HERMES_DEPLOY_PASSWORD;
    else process.env.HERMES_DEPLOY_PASSWORD = previous;
    await closeServer(server);
    rmSync(directory, { recursive: true, force: true });
  }
});

test("password SFTP upload preserves bytes", async () => {
  const directory = fixtureDir();
  const uploads = new Map();
  const server = await startServer({ uploads });
  const port = server.address().port;
  const localPath = path.join(directory, "release.tar.gz");
  const contents = Buffer.from([0, 1, 2, 3, 254, 255]);
  writeFileSync(localPath, contents);
  const previous = process.env.HERMES_DEPLOY_PASSWORD;
  process.env.HERMES_DEPLOY_PASSWORD = "fake-sentinel-password";
  try {
    const result = await uploadPasswordFile(
      { host: "127.0.0.1", port: String(port), user: "deploy" },
      localPath,
      "/tmp/release.tar.gz",
      { knownHostsFile: knownHostsFile(directory, port) },
    );
    assert.equal(result.status, 0);
    assert.deepEqual(uploads.get("/tmp/release.tar.gz"), contents);
  } finally {
    if (previous === undefined) delete process.env.HERMES_DEPLOY_PASSWORD;
    else process.env.HERMES_DEPLOY_PASSWORD = previous;
    await closeServer(server);
    rmSync(directory, { recursive: true, force: true });
  }
});
