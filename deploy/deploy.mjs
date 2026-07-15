#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { homedir, tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DEFAULT_HOST = "106.15.186.104";
const DEFAULT_USER = "root";
const DEFAULT_REMOTE_ROOT = "/opt/hermes";
const DEFAULT_NPM_REGISTRY = "https://registry.npmmirror.com";
const DEFAULT_IDENTITY_FILE = path.join(homedir(), ".ssh", "hermes_apiyi_ed25519");
const SSH_CONNECTION_ARGS = [
  "-o",
  "ConnectTimeout=15",
  "-o",
  "ServerAliveInterval=15",
  "-o",
  "ServerAliveCountMax=3",
];
const TAG_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;
const COMMIT_SHA_RE = /^[0-9a-f]{40}$/;
const DEFAULT_KEEP_RELEASES = 5;
const DEPLOY_NPM_WORKSPACES = ["web", "ui-tui"];

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");

function usage() {
  console.log(`Hermes tag-based bare-metal deploy tool

Usage:
  npm run deploy -- --create-tag v2026.7.3
  npm run deploy -- --tag v2026.7.3
  npm run deploy -- --ref <40-hex-commit-sha>
  npm run deploy -- --tag v2026.7.3 --dry-run

Options:
  --tag <tag>              Deploy an existing local git tag.
  --create-tag <tag>       Create an annotated tag at HEAD, push it, then deploy it.
  --ref <commit-sha>       Deploy an already-pushed immutable 40-hex commit SHA without creating a tag.
  --host <host>            SSH host. Default: ${DEFAULT_HOST}
  --user <user>            SSH user. Default: ${DEFAULT_USER}
  --port <port>            SSH port. Default: 22
  --identity-file <path>   SSH private key path. Default: ~/.ssh/hermes_apiyi_ed25519
  --remote-root <path>     Remote release root. Default: ${DEFAULT_REMOTE_ROOT}
  --allow-non-main         Allow creating a tag away from main.
  --allow-dirty            Allow deploying an existing tag with a dirty worktree.
  --force                  Deprecated and rejected; immutable releases are never replaced.
  --keep-releases <n>      Keep the newest n remote releases after deploy. Default: ${DEFAULT_KEEP_RELEASES}
  --no-prune-releases      Do not delete old remote release directories.
  --dry-run                Print commands without changing local or remote state.
  -h, --help               Show this help.

Authentication:
  Prefer SSH keys. For a temporary password-based deploy, set
  HERMES_DEPLOY_PASSWORD in your local environment and install sshpass.
  The password is never printed by this tool.

Environment:
  HERMES_DEPLOY_NPM_REGISTRY  npm registry used while building release artifacts.
                              Default: ${DEFAULT_NPM_REGISTRY}
`);
}

function parseArgs(argv) {
  const args = {
    host: process.env.HERMES_DEPLOY_HOST || DEFAULT_HOST,
    user: process.env.HERMES_DEPLOY_USER || DEFAULT_USER,
    port: process.env.HERMES_DEPLOY_PORT || "22",
    remoteRoot: process.env.HERMES_DEPLOY_REMOTE_ROOT || DEFAULT_REMOTE_ROOT,
    identityFile: process.env.HERMES_DEPLOY_IDENTITY_FILE || DEFAULT_IDENTITY_FILE,
    allowNonMain: false,
    allowDirty: false,
    force: false,
    keepReleases: DEFAULT_KEEP_RELEASES,
    pruneReleases: true,
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
      case "--ref":
        args.ref = next();
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
      case "--keep-releases":
        args.keepReleases = parsePositiveInteger(next(), arg);
        break;
      case "--no-prune-releases":
        args.pruneReleases = false;
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

  const sourceCount = [args.tag, args.createTag, args.ref].filter(Boolean).length;
  if (!args.help && sourceCount !== 1) {
    throw new Error("Pass exactly one of --tag, --create-tag, or --ref.");
  }

  args.sourceKind = args.ref ? "commit" : "tag";
  args.sourceRef = args.ref || args.createTag || args.tag;
  return args;
}

function parsePositiveInteger(value, name) {
  if (!/^\d+$/.test(value)) {
    throw new Error(`${name} requires a positive integer`);
  }
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < 1) {
    throw new Error(`${name} requires a positive integer`);
  }
  return parsed;
}

function formatCommand(command, commandArgs) {
  return [command, ...commandArgs.map((arg) => (/[\s'"$`\\]/.test(arg) ? JSON.stringify(arg) : arg))].join(" ");
}

function run(command, commandArgs, options = {}) {
  const { dryRun = false, input, env, quiet = false, cwd = repoRoot } = options;
  if (dryRun) {
    console.log(`[dry-run] ${formatCommand(command, commandArgs)}`);
    return { stdout: "", stderr: "", status: 0 };
  }

  if (!quiet) {
    console.log(`$ ${formatCommand(command, commandArgs)}`);
  }

  const result = spawnSync(command, commandArgs, {
    cwd,
    encoding: "utf8",
    input,
    stdio: input === undefined ? "pipe" : ["pipe", "pipe", "pipe"],
    env: env ? { ...process.env, ...env } : process.env,
  });

  if (result.error) {
    throw result.error;
  }
  const stdout = result.stdout?.trim() ?? "";
  const stderr = result.stderr?.trim() ?? "";
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

function validateImmutableCommitRef(ref) {
  if (!COMMIT_SHA_RE.test(ref)) {
    throw new Error("--ref requires a full lowercase 40-hex commit SHA.");
  }
}

function resolveImmutableCommit(ref) {
  validateImmutableCommitRef(ref);
  const resolved = runText("git", ["rev-parse", "--verify", `${ref}^{commit}`]);
  if (resolved !== ref) {
    throw new Error("--ref must resolve exactly to the supplied commit SHA.");
  }
  run("git", ["fetch", "--dry-run", "--no-tags", "origin", ref], { quiet: true });
  return resolved;
}

function releaseIdFor(args) {
  return args.sourceKind === "commit" ? `commit-${args.sourceCommit}` : args.sourceTag;
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

function createArchive(args, { dryRun }) {
  const { releaseId, sourceCommit, sourceKind, sourceTag } = args;
  const tmp = dryRun ? null : mkdtempSync(path.join(tmpdir(), "hermes-deploy-"));
  const buildDir = dryRun ? path.join(tmpdir(), `hermes-${releaseId}-artifact`) : path.join(tmp, "artifact");
  const archivePath = dryRun ? path.join(tmpdir(), `hermes-${releaseId}.tar.gz`) : path.join(tmp, `hermes-${releaseId}.tar.gz`);
  const sourceArchive = dryRun ? path.join(tmpdir(), `hermes-${releaseId}.tar`) : path.join(tmp, `hermes-${releaseId}.tar`);

  if (!dryRun) {
    mkdirSync(buildDir, { recursive: true });
  }
  const archiveEnv = { COPYFILE_DISABLE: "1" };
  run("git", ["archive", "--format=tar", "--output", sourceArchive, sourceCommit], { dryRun, env: archiveEnv });
  run("tar", ["-xf", sourceArchive, "-C", buildDir], { dryRun, env: archiveEnv });
  if (!dryRun) {
    writeFileSync(
      path.join(buildDir, ".hermes-release.json"),
      `${JSON.stringify({ schemaVersion: 1, releaseId, source: { kind: sourceKind, commit: sourceCommit, tag: sourceTag ?? null } }, null, 2)}\n`,
      "utf8",
    );
  }

  buildArtifact(buildDir, { dryRun });
  run(
    "tar",
    [
      "-czf",
      archivePath,
      "--exclude=._*",
      "--exclude=*/._*",
      "--exclude=./node_modules",
      "--exclude=./web/node_modules",
      "--exclude=./ui-tui/node_modules",
      "--exclude=./apps/*/node_modules",
      "-C",
      buildDir,
      ".",
    ],
    { dryRun, env: archiveEnv },
  );
  return { tmp, archivePath };
}

function buildArtifact(buildDir, { dryRun }) {
  const webOutDir = path.join(buildDir, "hermes_cli/web_dist");
  const tuiOutFile = path.join(buildDir, "ui-tui/dist/entry.js");
  const npmRegistry = process.env.HERMES_DEPLOY_NPM_REGISTRY || DEFAULT_NPM_REGISTRY;
  run(
    "npm",
    [
      "install",
      ...DEPLOY_NPM_WORKSPACES.flatMap((workspace) => ["--workspace", workspace]),
      "--include-workspace-root=false",
      "--prefer-offline",
      "--no-audit",
      "--registry",
      npmRegistry,
    ],
    { dryRun, cwd: buildDir },
  );
  run("npm", ["run", "build", "--workspace", "web"], {
    dryRun,
    cwd: buildDir,
    env: { HERMES_WEB_OUT_DIR: webOutDir },
  });
  run("npm", ["run", "build", "--workspace", "ui-tui"], {
    dryRun,
    cwd: buildDir,
    env: { HERMES_TUI_OUTFILE: tuiOutFile },
  });
  run("test", ["-f", path.join(buildDir, "hermes_cli/web_dist/index.html")], { dryRun, cwd: buildDir });
  run("test", ["-f", path.join(buildDir, "ui-tui/dist/entry.js")], { dryRun, cwd: buildDir });
}

function sshBaseArgs(args) {
  const base = ["-p", args.port, "-o", "BatchMode=no", ...SSH_CONNECTION_ARGS];
  if (args.identityFile) {
    base.push("-i", args.identityFile);
  }
  return base;
}

function scpBaseArgs(args) {
  const base = ["-P", args.port, "-o", "BatchMode=no", ...SSH_CONNECTION_ARGS];
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
release_id="$2"
source_commit="$3"
source_kind="$4"
source_tag="$5"
[ "$source_tag" = "-" ] && source_tag=""
archive="$6"
keep_releases="$7"
prune_releases="$8"
tmp_dir="$remote_root/tmp"
releases_dir="$remote_root/releases"
release="$releases_dir/$release_id"
release_tmp="$releases_dir/.$release_id.tmp.$$"
release_lock="$releases_dir/.$release_id.lock"
current="$remote_root/current"
shared="$remote_root/shared"
env_file="$shared/.env"
hermes_home="$shared/.hermes"
runner="$shared/hermes-service-runner.sh"
old_current_target=""
new_current_target=""
release_target=""

gateway_unit="/etc/systemd/system/hermes-gateway.service"
dashboard_unit="/etc/systemd/system/hermes-dashboard.service"

cleanup_release_tmp() {
  rm -rf -- "$release_tmp"
  rm -f -- "$archive"
  rmdir -- "$release_lock" 2>/dev/null || true
}
trap cleanup_release_tmp EXIT

is_release_name() {
  [[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]
}

resolved_path() {
  readlink -f "$1" 2>/dev/null || true
}

is_protected_release() {
  local candidate="$1"
  local candidate_target
  candidate_target="$(resolved_path "$candidate")"
  [ -n "$candidate_target" ] || return 1
  [ "$candidate_target" = "$release_target" ] && return 0
  [ -n "$old_current_target" ] && [ "$candidate_target" = "$old_current_target" ] && return 0
  [ -n "$new_current_target" ] && [ "$candidate_target" = "$new_current_target" ] && return 0
  return 1
}

prune_old_releases() {
  if [ "$prune_releases" != "1" ]; then
    echo "Release pruning disabled; keeping all directories under $releases_dir"
    return
  fi

  local -a ordered=()
  local item candidate name mtime keep_count=0
  shopt -s nullglob
  for candidate in "$releases_dir"/*; do
    [ -d "$candidate" ] || continue
    [ ! -L "$candidate" ] || continue
    name="${"${"}candidate##*/}"
    if ! is_release_name "$name"; then
      echo "Skipping non-tag release directory during prune: $candidate"
      continue
    fi
    mtime="$(stat -c %Y "$candidate" 2>/dev/null || echo 0)"
    ordered+=("${"${"}mtime}"$'\t'"${"${"}name}")
  done
  shopt -u nullglob

  if [ "${"${"}#ordered[@]}" -eq 0 ]; then
    echo "No release directories found to prune."
    return
  fi

  while IFS=$'\t' read -r _ name; do
    [ -n "$name" ] || continue
    candidate="$releases_dir/$name"
    if is_protected_release "$candidate"; then
      echo "Keeping protected release: $candidate"
      continue
    fi
    if [ "$keep_count" -lt "$keep_releases" ]; then
      keep_count=$((keep_count + 1))
      echo "Keeping recent release: $candidate"
      continue
    fi
    echo "Pruning old release: $candidate"
    rm -rf -- "$candidate"
  done < <(printf '%s\n' "${"${"}ordered[@]}" | sort -rn)
}

if ! is_release_name "$release_id"; then
  echo "Invalid release ID on remote: $release_id" >&2
  exit 1
fi
if ! [[ "$source_commit" =~ ^[0-9a-f]{40}$ ]] || [[ "$source_kind" != "tag" && "$source_kind" != "commit" ]]; then
  echo "Invalid immutable release source" >&2
  exit 1
fi
if [ "$source_kind" = "tag" ] && ! is_release_name "$source_tag"; then
  echo "Invalid release tag source" >&2
  exit 1
fi
if [ "$source_kind" = "commit" ] && [ -n "$source_tag" ]; then
  echo "Commit release must not include a tag" >&2
  exit 1
fi
if ! [[ "$keep_releases" =~ ^[0-9]+$ ]] || [ "$keep_releases" -lt 1 ]; then
  echo "Invalid keep_releases value: $keep_releases" >&2
  exit 1
fi

for required in tar systemctl sha256sum readlink stat sort mv; do
  if ! command -v "$required" >/dev/null 2>&1; then
    echo "Missing required command: $required" >&2
    exit 1
  fi
done

mkdir -p "$releases_dir" "$tmp_dir" "$hermes_home"
if [ ! -f "$env_file" ]; then
  umask 077
  : > "$env_file"
fi
chmod 600 "$env_file" 2>/dev/null || true
chmod 700 "$hermes_home" 2>/dev/null || true

if ! mkdir -- "$release_lock"; then
  echo "Release is already being deployed or requires investigation: $release_id" >&2
  exit 1
fi
if [ -L "$current" ]; then
  old_current_target="$(resolved_path "$current")"
fi

expected_manifest="{\"schemaVersion\":1,\"releaseId\":\"$release_id\",\"source\":{\"kind\":\"$source_kind\",\"commit\":\"$source_commit\",\"tag\":$(if [ -n "$source_tag" ]; then printf '\"%s\"' "$source_tag"; else printf 'null'; fi)}}"
if [ -e "$release" ]; then
  actual_manifest="$(tr -d '\n[:space:]' < "$release/.hermes-release.json" 2>/dev/null || true)"
  if [ "$actual_manifest" != "$expected_manifest" ]; then
    echo "Existing release does not match immutable source: $release" >&2
    exit 1
  fi
  echo "Remote release already exists with matching source, reusing: $release"
else
  mkdir -p "$release_tmp"
  tar -xzf "$archive" -C "$release_tmp"
  actual_manifest="$(tr -d '\n[:space:]' < "$release_tmp/.hermes-release.json" 2>/dev/null || true)"
  if [ "$actual_manifest" != "$expected_manifest" ]; then
    echo "Release manifest does not match immutable source" >&2
    exit 1
  fi
  test -f "$release_tmp/hermes_cli/web_dist/index.html"
  test -f "$release_tmp/ui-tui/dist/entry.js"
  mv -- "$release_tmp" "$release"
fi
rm -f -- "$archive"

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export UV_NO_CONFIG=1
export HERMES_HOME="$hermes_home"

test -f "$release/hermes_cli/web_dist/index.html"
test -f "$release/ui-tui/dist/entry.js"

venv="$shared/venv"
lock_hash="$(sha256sum "$release/uv.lock" | cut -d ' ' -f1)"
lock_stamp="$venv/.hermes-uv-lock.sha256"
installed_hash=""
if [ -f "$lock_stamp" ]; then
  installed_hash="$(cat "$lock_stamp")"
fi

if [ ! -x "$venv/bin/python" ] || [ "$installed_hash" != "$lock_hash" ]; then
  echo "Python dependencies need bootstrap/update for uv.lock $lock_hash"
  if ! command -v uv >/dev/null 2>&1; then
    if ! command -v curl >/dev/null 2>&1; then
      echo "Missing required command: curl (needed to install uv)" >&2
      exit 1
    fi
    echo "Installing uv..."
    uv_installer="$(mktemp)"
    curl -LsSf https://astral.sh/uv/install.sh -o "$uv_installer"
    sh "$uv_installer"
    rm -f "$uv_installer"
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  fi
  uv python find 3.11 >/dev/null 2>&1 || uv python install 3.11
  cd "$release"
  UV_PROJECT_ENVIRONMENT="$venv" uv sync --extra all --locked
  printf '%s\n' "$lock_hash" > "$lock_stamp"
else
  echo "Python dependencies unchanged; reusing $venv"
fi

ln -sfnT "$release" "$current"
release_target="$(resolved_path "$release")"
new_current_target="$(resolved_path "$current")"

cat > "$runner" <<'RUNNER'
#!/usr/bin/env bash
set -euo pipefail
remote_root="${"${"}HERMES_REMOTE_ROOT:-/opt/hermes}"
current="$remote_root/current"
shared="$remote_root/shared"
env_file="${"${"}HERMES_ENV_FILE:-$shared/.env}"
hermes_home="${"${"}HERMES_HOME:-$shared/.hermes}"
venv="${"${"}VIRTUAL_ENV:-$shared/venv}"

export HERMES_HOME="$hermes_home"
export VIRTUAL_ENV="$venv"
export PATH="$venv/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export PYTHONUNBUFFERED=1

if [ -f "$env_file" ]; then
  set -a
  . "$env_file"
  set +a
fi

cd "$current"
exec "$venv/bin/python" -m hermes_cli.main "$@"
RUNNER
chmod 0755 "$runner"

cat > "$gateway_unit" <<UNIT
[Unit]
Description=Hermes Gateway
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
Environment=HERMES_REMOTE_ROOT=$remote_root
Environment=HERMES_HOME=$hermes_home
Environment=HERMES_ENV_FILE=$env_file
Environment=VIRTUAL_ENV=$shared/venv
WorkingDirectory=$current
ExecStart=$runner gateway run --replace
ExecReload=/bin/kill -USR1 \$MAINPID
Restart=always
RestartSec=5
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=120
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

cat > "$dashboard_unit" <<UNIT
[Unit]
Description=Hermes Dashboard
After=network-online.target hermes-gateway.service
Wants=network-online.target hermes-gateway.service
StartLimitIntervalSec=0

[Service]
Type=simple
Environment=HERMES_REMOTE_ROOT=$remote_root
Environment=HERMES_HOME=$hermes_home
Environment=HERMES_ENV_FILE=$env_file
Environment=VIRTUAL_ENV=$shared/venv
WorkingDirectory=$current
ExecStart=$runner dashboard --host 127.0.0.1 --port 9119 --no-open --skip-build --require-auth
Restart=always
RestartSec=5
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable hermes-gateway.service hermes-dashboard.service
systemctl restart hermes-gateway.service hermes-dashboard.service
systemctl is-active --quiet hermes-gateway.service
systemctl is-active --quiet hermes-dashboard.service
systemctl --no-pager --full status hermes-gateway.service hermes-dashboard.service || true

prune_old_releases

echo "Hermes deployed from $source_kind source $source_commit at $release"
echo "Remote archive cleaned: $archive"
if [ "$prune_releases" = "1" ]; then
  echo "Release retention: kept newest $keep_releases releases plus protected current/deployed releases"
else
  echo "Release retention: pruning disabled"
fi
`;
}

function deployArchive(args, archivePath) {
  const remoteRoot = args.remoteRoot.replace(/\/+$/, "");
  const stagingId = args.dryRun ? "dry-run" : randomUUID();
  const remoteArchive = `${remoteRoot}/tmp/hermes-${args.releaseId}-${stagingId}.tar.gz`;

  runSsh(args, ["mkdir", "-p", `${remoteRoot}/tmp`, `${remoteRoot}/releases`, `${remoteRoot}/shared/.hermes`]);
  runScp(args, archivePath, remoteArchive);
  runSsh(
    args,
    [
      "bash",
      "-s",
      "--",
      remoteRoot,
      args.releaseId,
      args.sourceCommit,
      args.sourceKind,
      args.sourceTag ?? "-",
      remoteArchive,
      String(args.keepReleases),
      args.pruneReleases ? "1" : "0",
    ],
    { input: remoteDeployScript() },
  );
}

function printSummary(args) {
  const remoteRoot = args.remoteRoot.replace(/\/+$/, "");
  const target = `${args.user}@${args.host}`;
  console.log(`\nDeploy target: ${target}:${remoteRoot}`);
  console.log(`${args.sourceKind === "commit" ? "Commit SHA" : "Tag"}: ${args.sourceKind === "commit" ? args.sourceCommit : args.sourceTag}`);
  console.log(`Current symlink: ${remoteRoot}/current -> ${remoteRoot}/releases/${args.releaseId}`);
  console.log(`State dir: ${remoteRoot}/shared/.hermes`);
  console.log(`Env file: ${remoteRoot}/shared/.env`);
  console.log(`Services: hermes-gateway.service, hermes-dashboard.service`);
  console.log("Remote staging archive is uniquely named and removed after extraction.");
  console.log(
    args.pruneReleases
      ? `Release retention: keep newest ${args.keepReleases} releases plus protected current/deployed releases`
      : `Release retention: disabled (--no-prune-releases)`,
  );
  console.log(`Status: ssh ${target} 'systemctl status --no-pager hermes-gateway hermes-dashboard'`);
  console.log("Rollback example: npm run deploy -- --tag <previous-tag>");
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    usage();
    return;
  }

  requireBinary("git");
  requireBinary("ssh");
  requireBinary("scp");

  if (args.force) {
    throw new Error("--force is no longer supported for immutable releases.");
  }

  if (args.createTag) {
    validateTag(args.createTag);
    assertMainBranch({ allowNonMain: args.allowNonMain });
    assertCleanWorktree({ allowDirty: false, dryRun: args.dryRun });
    createAnnotatedTag(args.createTag, { dryRun: args.dryRun });
    args.sourceTag = args.createTag;
    args.sourceCommit = args.dryRun
      ? runText("git", ["rev-parse", "--verify", "HEAD^{commit}"])
      : runText("git", ["rev-parse", "--verify", `${args.createTag}^{commit}`]);
  } else if (args.ref) {
    if (args.force || args.allowDirty) {
      throw new Error("--ref does not allow --force or --allow-dirty.");
    }
    assertCleanWorktree({ allowDirty: false, dryRun: args.dryRun });
    validateImmutableCommitRef(args.ref);
    args.sourceCommit = args.dryRun ? args.ref : resolveImmutableCommit(args.ref);
  } else {
    validateTag(args.tag);
    assertCleanWorktree({ allowDirty: args.allowDirty, dryRun: args.dryRun });
    if (!tagExists(args.tag)) {
      throw new Error(`Tag does not exist locally: ${args.tag}. Run 'git fetch --tags' first if needed.`);
    }
    args.sourceTag = args.tag;
    args.sourceCommit = runText("git", ["rev-parse", "--verify", `${args.tag}^{commit}`]);
  }

  args.releaseId = releaseIdFor(args);
  const { tmp, archivePath } = createArchive(args, { dryRun: args.dryRun });
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
