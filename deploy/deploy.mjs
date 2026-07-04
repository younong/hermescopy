#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DEFAULT_HOST = "106.15.186.104";
const DEFAULT_USER = "root";
const DEFAULT_REMOTE_ROOT = "/opt/hermes";
const TAG_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");

function usage() {
  console.log(`Hermes tag-based deploy tool

Usage:
  npm run deploy -- --create-tag v2026.7.3
  npm run deploy -- --tag v2026.7.3
  npm run deploy -- --tag v2026.7.3 --dry-run

Options:
  --tag <tag>              Deploy an existing local git tag.
  --create-tag <tag>       Create an annotated tag at HEAD, push it, then deploy it.
  --host <host>            SSH host. Default: ${DEFAULT_HOST}
  --user <user>            SSH user. Default: ${DEFAULT_USER}
  --port <port>            SSH port. Default: 22
  --identity-file <path>   SSH private key path.
  --remote-root <path>     Remote release root. Default: ${DEFAULT_REMOTE_ROOT}
  --allow-non-main         Allow creating a tag away from main.
  --allow-dirty            Allow deploying an existing tag with a dirty worktree.
  --force                  Replace an existing remote release directory for the tag.
  --dry-run                Print commands without changing local or remote state.
  -h, --help               Show this help.

Authentication:
  Prefer SSH keys. For a temporary password-based deploy, set
  HERMES_DEPLOY_PASSWORD in your local environment and install sshpass.
  The password is never printed by this tool.
`);
}

function parseArgs(argv) {
  const args = {
    host: process.env.HERMES_DEPLOY_HOST || DEFAULT_HOST,
    user: process.env.HERMES_DEPLOY_USER || DEFAULT_USER,
    port: process.env.HERMES_DEPLOY_PORT || "22",
    remoteRoot: process.env.HERMES_DEPLOY_REMOTE_ROOT || DEFAULT_REMOTE_ROOT,
    identityFile: process.env.HERMES_DEPLOY_IDENTITY_FILE || "",
    allowNonMain: false,
    allowDirty: false,
    force: false,
    dryRun: false,
  };

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => {
      const value = argv[i + 1];
      if (!value || value.startsWith("--")) {
        throw new Error(`${arg} requires a value`);
      }
      i += 1;
      return value;
    };

    switch (arg) {
      case "--tag":
        args.tag = next();
        break;
      case "--create-tag":
        args.createTag = next();
        break;
      case "--host":
        args.host = next();
        break;
      case "--user":
        args.user = next();
        break;
      case "--port":
        args.port = next();
        break;
      case "--identity-file":
        args.identityFile = next();
        break;
      case "--remote-root":
        args.remoteRoot = next();
        break;
      case "--allow-non-main":
        args.allowNonMain = true;
        break;
      case "--allow-dirty":
        args.allowDirty = true;
        break;
      case "--force":
        args.force = true;
        break;
      case "--dry-run":
        args.dryRun = true;
        break;
      case "-h":
      case "--help":
        args.help = true;
        break;
      default:
        throw new Error(`Unknown argument: ${arg}`);
    }
  }

  if (!args.help && Boolean(args.tag) === Boolean(args.createTag)) {
    throw new Error("Pass exactly one of --tag or --create-tag.");
  }

  args.deployTag = args.createTag || args.tag;
  return args;
}

function formatCommand(command, commandArgs) {
  return [command, ...commandArgs.map((arg) => (/[\s'"$`\\]/.test(arg) ? JSON.stringify(arg) : arg))].join(" ");
}

function run(command, commandArgs, options = {}) {
  const { dryRun = false, input, env, quiet = false } = options;
  if (dryRun) {
    console.log(`[dry-run] ${formatCommand(command, commandArgs)}`);
    return { stdout: "", stderr: "", status: 0 };
  }

  if (!quiet) {
    console.log(`$ ${formatCommand(command, commandArgs)}`);
  }

  const result = spawnSync(command, commandArgs, {
    cwd: repoRoot,
    encoding: "utf8",
    input,
    stdio: input === undefined ? "pipe" : ["pipe", "pipe", "pipe"],
    env: env ? { ...process.env, ...env } : process.env,
  });

  if (result.error) {
    throw result.error;
  }
  const stdout = result.stdout?.trim();
  const stderr = result.stderr?.trim();
  if (result.status !== 0) {
    throw new Error(`${formatCommand(command, commandArgs)} failed${stderr ? `:\n${stderr}` : stdout ? `:\n${stdout}` : ""}`);
  }
  if (!quiet) {
    if (stdout) {
      console.log(stdout);
    }
    if (stderr) {
      console.error(stderr);
    }
  }
  return result;
}

function runText(command, commandArgs) {
  return run(command, commandArgs, { quiet: true }).stdout.trim();
}

function requireBinary(name) {
  const result = spawnSync("command", ["-v", name], {
    encoding: "utf8",
    shell: true,
  });
  if (result.status !== 0) {
    throw new Error(`Required command not found: ${name}`);
  }
}

function validateTag(tag) {
  if (!TAG_RE.test(tag)) {
    throw new Error(`Invalid tag '${tag}'. Use letters, numbers, dots, underscores, and dashes only.`);
  }
}

function assertCleanWorktree({ allowDirty, dryRun = false }) {
  const status = runText("git", ["status", "--porcelain"]);
  if (status && dryRun) {
    console.log("! Working tree has local changes; continuing because this is a dry run.");
    return;
  }
  if (status && !allowDirty) {
    throw new Error("Working tree is not clean. Commit/stash changes, or use --allow-dirty when deploying an existing tag.");
  }
  if (status) {
    console.log("! Working tree has local changes; continuing because --allow-dirty was set.");
  }
}

function assertMainBranch({ allowNonMain }) {
  const branch = runText("git", ["branch", "--show-current"]);
  if (branch !== "main" && !allowNonMain) {
    throw new Error(`Current branch is '${branch || "detached HEAD"}', not 'main'. Use --allow-non-main to override.`);
  }
}

function tagExists(tag) {
  const result = spawnSync("git", ["rev-parse", "--quiet", "--verify", `refs/tags/${tag}`], {
    cwd: repoRoot,
    encoding: "utf8",
  });
  return result.status === 0;
}

function createAnnotatedTag(tag, { dryRun }) {
  if (tagExists(tag)) {
    throw new Error(`Tag already exists: ${tag}`);
  }
  run("git", ["tag", "-a", tag, "-m", `Hermes deploy ${tag}`], { dryRun });
  run("git", ["push", "origin", "HEAD", "--tags"], { dryRun });
}

function createArchive(tag, { dryRun }) {
  if (dryRun) {
    const archivePath = path.join(tmpdir(), `hermes-${tag}.tar.gz`);
    run("git", ["archive", "--format=tar.gz", "--output", archivePath, tag], { dryRun });
    return { tmp: null, archivePath };
  }

  const tmp = mkdtempSync(path.join(tmpdir(), "hermes-deploy-"));
  const archivePath = path.join(tmp, `hermes-${tag}.tar.gz`);
  run("git", ["archive", "--format=tar.gz", "--output", archivePath, tag], { dryRun });
  return { tmp, archivePath };
}

function sshBaseArgs(args) {
  const base = ["-p", args.port, "-o", "BatchMode=no"];
  if (args.identityFile) {
    base.push("-i", args.identityFile);
  }
  return base;
}

function scpBaseArgs(args) {
  const base = ["-P", args.port, "-o", "BatchMode=no"];
  if (args.identityFile) {
    base.push("-i", args.identityFile);
  }
  return base;
}

function withSshpass(command, commandArgs, { dryRun }) {
  if (!process.env.HERMES_DEPLOY_PASSWORD) {
    return { command, commandArgs, env: undefined };
  }
  if (!dryRun) {
    requireBinary("sshpass");
  }
  return {
    command: "sshpass",
    commandArgs: ["-e", command, ...commandArgs],
    env: { SSHPASS: process.env.HERMES_DEPLOY_PASSWORD },
  };
}

function remoteTarget(args) {
  return `${args.user}@${args.host}`;
}

function runSsh(args, remoteArgs, options = {}) {
  if (args.dryRun && options.input) {
    console.log(`[dry-run] remote script:\n${options.input}`);
  }
  const sshArgs = [...sshBaseArgs(args), remoteTarget(args), ...remoteArgs];
  const wrapped = withSshpass("ssh", sshArgs, { dryRun: args.dryRun });
  if (args.dryRun && process.env.HERMES_DEPLOY_PASSWORD) {
    console.log("[dry-run] HERMES_DEPLOY_PASSWORD is set; would run SSH through sshpass -e (password hidden).");
  }
  return run(wrapped.command, wrapped.commandArgs, {
    dryRun: args.dryRun,
    input: options.input,
    env: wrapped.env,
  });
}

function runScp(args, localPath, remotePath) {
  const scpArgs = [...scpBaseArgs(args), localPath, `${remoteTarget(args)}:${remotePath}`];
  const wrapped = withSshpass("scp", scpArgs, { dryRun: args.dryRun });
  if (args.dryRun && process.env.HERMES_DEPLOY_PASSWORD) {
    console.log("[dry-run] HERMES_DEPLOY_PASSWORD is set; would run SCP through sshpass -e (password hidden).");
  }
  return run(wrapped.command, wrapped.commandArgs, {
    dryRun: args.dryRun,
    env: wrapped.env,
  });
}

function remoteDeployScript() {
  return String.raw`set -euo pipefail
remote_root="$1"
tag="$2"
force="$3"
archive="$remote_root/tmp/hermes-$tag.tar.gz"
release="$remote_root/releases/$tag"
current="$remote_root/current"
shared="$remote_root/shared"

mkdir -p "$remote_root/releases" "$remote_root/tmp" "$shared/.hermes"
if [ ! -f "$shared/.env" ]; then
  umask 077
  : > "$shared/.env"
fi

if [ -e "$release" ]; then
  if [ "$force" = "1" ]; then
    rm -rf "$release"
  else
    echo "Remote release already exists, reusing: $release"
  fi
fi

if [ ! -e "$release" ]; then
  mkdir -p "$release"
  tar -xzf "$archive" -C "$release"
fi

ln -sfnT "$release" "$current"
cd "$current"

export HERMES_DATA_DIR="$shared/.hermes"
export HERMES_ENV_FILE="$shared/.env"
export HERMES_UID="$(id -u)"
export HERMES_GID="$(id -g)"

docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml up -d --build
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml ps

echo "Hermes deployed from tag $tag at $release"
`;
}

function deployArchive(args, archivePath) {
  const remoteRoot = args.remoteRoot.replace(/\/+$/, "");
  const remoteArchive = `${remoteRoot}/tmp/hermes-${args.deployTag}.tar.gz`;

  runSsh(args, ["mkdir", "-p", `${remoteRoot}/tmp`, `${remoteRoot}/releases`, `${remoteRoot}/shared/.hermes`]);
  runScp(args, archivePath, remoteArchive);
  runSsh(args, ["bash", "-s", "--", remoteRoot, args.deployTag, args.force ? "1" : "0"], {
    input: remoteDeployScript(),
  });
}

function printSummary(args) {
  const remoteRoot = args.remoteRoot.replace(/\/+$/, "");
  console.log(`\nDeploy target: ${args.user}@${args.host}:${remoteRoot}`);
  console.log(`Tag: ${args.deployTag}`);
  console.log(`Current symlink: ${remoteRoot}/current -> ${remoteRoot}/releases/${args.deployTag}`);
  console.log(`Rollback example: npm run deploy -- --tag <previous-tag>`);
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    usage();
    return;
  }

  validateTag(args.deployTag);
  requireBinary("git");
  requireBinary("ssh");
  requireBinary("scp");

  if (args.createTag) {
    assertMainBranch({ allowNonMain: args.allowNonMain });
    assertCleanWorktree({ allowDirty: false, dryRun: args.dryRun });
    createAnnotatedTag(args.createTag, { dryRun: args.dryRun });
  } else {
    assertCleanWorktree({ allowDirty: args.allowDirty, dryRun: args.dryRun });
    if (!tagExists(args.tag)) {
      throw new Error(`Tag does not exist locally: ${args.tag}. Run 'git fetch --tags' first if needed.`);
    }
  }

  const { tmp, archivePath } = createArchive(args.deployTag, { dryRun: args.dryRun });
  try {
    deployArchive(args, archivePath);
    printSummary(args);
  } finally {
    if (tmp && !args.dryRun) {
      rmSync(tmp, { recursive: true, force: true });
    }
  }
}

try {
  main();
} catch (error) {
  console.error(`deploy failed: ${error.message}`);
  process.exit(1);
}
