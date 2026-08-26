import crossSpawn from "cross-spawn";
import {
  accessSync,
  constants as fsConstants,
  existsSync,
  renameSync,
  statSync,
} from "node:fs";
import path from "node:path";

const MAX_OUTPUT_BYTES = 64 * 1024 * 1024;
const DEPLOY_PASSWORD_NAME = "HERMES_DEPLOY_PASSWORD";

function commandCandidates(name, env) {
  if (path.isAbsolute(name) || name.includes(path.sep)) {
    return [name];
  }
  if (process.platform !== "win32" || path.extname(name)) {
    return [name];
  }
  const pathExt = (env.PATHEXT || ".COM;.EXE;.BAT;.CMD")
    .split(";")
    .filter(Boolean);
  return [name, ...pathExt.map((extension) => `${name}${extension.toLowerCase()}`)];
}

export function resolveLocalCommand(name, { env = process.env } = {}) {
  const searchPath = env.PATH || env.Path || env.path || "";
  const directories = path.isAbsolute(name) ? [""] : searchPath.split(path.delimiter);
  const candidates = commandCandidates(name, env);
  const accessMode = process.platform === "win32" ? fsConstants.F_OK : fsConstants.X_OK;

  for (const directory of directories) {
    for (const candidate of candidates) {
      const resolved = directory ? path.resolve(directory, candidate) : candidate;
      try {
        accessSync(resolved, accessMode);
        if (statSync(resolved).isFile()) {
          return resolved;
        }
      } catch {
        // Continue through PATH/PATHEXT candidates.
      }
    }
  }
  return null;
}

export function requireLocalCommand(name, options = {}) {
  const resolved = resolveLocalCommand(name, options);
  if (!resolved) {
    throw new Error(`Required command not found: ${name}`);
  }
  return resolved;
}

export function sanitizedChildEnv(overrides = {}, { includeDeployPassword = false } = {}) {
  const env = { ...process.env, ...overrides };
  if (!includeDeployPassword) {
    delete env[DEPLOY_PASSWORD_NAME];
  }
  return env;
}

export function redactDeploySecret(value) {
  const text = String(value ?? "");
  const password = process.env[DEPLOY_PASSWORD_NAME];
  return password ? text.split(password).join("[REDACTED]") : text;
}

export function formatCommand(command, commandArgs) {
  return [command, ...commandArgs.map((arg) => (/\s|'|"|\$|`|\\/.test(arg) ? JSON.stringify(arg) : arg))].join(" ");
}

export function runLocal(command, commandArgs, options = {}) {
  const {
    cwd,
    dryRun = false,
    input,
    env,
    quiet = false,
    displayCommand = command,
  } = options;
  if (dryRun) {
    console.log(`[dry-run] ${formatCommand(displayCommand, commandArgs)}`);
    return { stdout: "", stderr: "", status: 0 };
  }

  const childEnv = sanitizedChildEnv(env);
  const executable = requireLocalCommand(command, { env: childEnv });
  if (!quiet) {
    console.log(`$ ${formatCommand(displayCommand, commandArgs)}`);
  }

  const result = crossSpawn.sync(executable, commandArgs, {
    cwd,
    encoding: "utf8",
    input,
    stdio: ["pipe", "pipe", "pipe"],
    env: childEnv,
    maxBuffer: MAX_OUTPUT_BYTES,
  });
  if (result.error) {
    throw new Error(redactDeploySecret(result.error.message), { cause: result.error });
  }

  const stdout = redactDeploySecret(result.stdout?.trim() ?? "");
  const stderr = redactDeploySecret(result.stderr?.trim() ?? "");
  if (result.status !== 0) {
    const error = new Error(
      `${formatCommand(displayCommand, commandArgs)} failed${stderr ? `:\n${stderr}` : stdout ? `:\n${stdout}` : ""}`,
    );
    error.commandResult = { ...result, stdout, stderr };
    throw error;
  }
  if (!quiet) {
    if (stdout) console.log(stdout);
    if (stderr) console.error(stderr);
  }
  return { ...result, stdout, stderr };
}

export function runLocalText(command, commandArgs, options = {}) {
  return runLocal(command, commandArgs, { ...options, quiet: true }).stdout.trim();
}

function parsePythonVersion(stdout) {
  const match = String(stdout).match(/^(\d+)\.(\d+)\.(\d+)$/);
  return match ? match.slice(1).map(Number) : null;
}

export function resolvePythonCommand({ env = process.env } = {}) {
  const candidates = process.platform === "win32"
    ? [
        { command: "py", argsPrefix: ["-3.13"] },
        { command: "py", argsPrefix: ["-3.12"] },
        { command: "py", argsPrefix: ["-3.11"] },
        { command: "python3", argsPrefix: [] },
        { command: "python", argsPrefix: [] },
      ]
    : [
        { command: "python3", argsPrefix: [] },
        { command: "python", argsPrefix: [] },
      ];

  for (const candidate of candidates) {
    if (!resolveLocalCommand(candidate.command, { env })) continue;
    try {
      const result = runLocal(
        candidate.command,
        [
          ...candidate.argsPrefix,
          "-c",
          "import sys; print('.'.join(map(str, sys.version_info[:3])))",
        ],
        { env, quiet: true },
      );
      const version = parsePythonVersion(result.stdout);
      if (version && version[0] === 3 && version[1] >= 11 && version[1] < 14) {
        return { ...candidate, version: version.join(".") };
      }
    } catch {
      // Try the next supported Python launcher.
    }
  }
  throw new Error("Required Python version not found: install Python >=3.11,<3.14.");
}

export function moveDirectory(source, target) {
  if (!existsSync(source) || !statSync(source).isDirectory()) {
    throw new Error(`Expected directory was not produced: ${source}`);
  }
  if (existsSync(target)) {
    throw new Error(`Refusing to replace existing directory: ${target}`);
  }
  renameSync(source, target);
}

export function requireFile(filePath, description) {
  if (!existsSync(filePath) || !statSync(filePath).isFile()) {
    throw new Error(`${description} was not produced: ${filePath}`);
  }
}
