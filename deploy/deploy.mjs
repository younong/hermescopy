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
const DEFAULT_PYTHON_PACKAGE_INDEX = "https://mirrors.aliyun.com/pypi/simple";
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
const DEFAULT_DASHBOARD_PUBLIC_URL = "https://abinllm.xyz/hermes";
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
  --create-tag <tag>       Rebase onto origin/main, push the branch and one annotated tag, then deploy it.
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
  --dashboard-public-url <url>
                           Public dashboard URL used by the trusted loopback proxy.
                           Default: ${DEFAULT_DASHBOARD_PUBLIC_URL}
  --migrate-nginx-hermes   Explicitly replace the recognized legacy Hermes Nginx
                           auth block after the new internal auth gate is healthy.
  --provision-powerpoint-deps
                           Add reviewed LibreOffice/font host prerequisites before
                           building the immutable PowerPoint executor runtime.
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
    dashboardPublicUrl:
      process.env.HERMES_DEPLOY_DASHBOARD_PUBLIC_URL || DEFAULT_DASHBOARD_PUBLIC_URL,
    migrateNginxHermes: false,
    provisionPowerpointDeps: false,
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
      case "--dashboard-public-url":
        args.dashboardPublicUrl = next();
        break;
      case "--migrate-nginx-hermes":
        args.migrateNginxHermes = true;
        break;
      case "--provision-powerpoint-deps":
        args.provisionPowerpointDeps = true;
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

  let publicUrl;
  try {
    publicUrl = new URL(args.dashboardPublicUrl);
  } catch {
    throw new Error("--dashboard-public-url must be an absolute http(s) URL.");
  }
  if (
    !["http:", "https:"].includes(publicUrl.protocol) ||
    !publicUrl.host ||
    publicUrl.username ||
    publicUrl.password ||
    publicUrl.search ||
    publicUrl.hash
  ) {
    throw new Error(
      "--dashboard-public-url must be an absolute http(s) URL without credentials, query, or fragment.",
    );
  }
  args.dashboardPublicUrl = args.dashboardPublicUrl.replace(/\/+$/, "");
  args.dashboardPublicHost = publicUrl.host;
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
    maxBuffer: 64 * 1024 * 1024,
  });

  if (result.error) {
    throw result.error;
  }
  const stdout = result.stdout?.trim() ?? "";
  const stderr = result.stderr?.trim() ?? "";
  if (result.status !== 0) {
    const error = new Error(`${formatCommand(command, commandArgs)} failed${stderr ? `:\n${stderr}` : stdout ? `:\n${stdout}` : ""}`);
    error.commandResult = result;
    throw error;
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

function runText(command, commandArgs, options = {}) {
  return run(command, commandArgs, { ...options, quiet: true }).stdout.trim();
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

function assertCleanWorktree({ allowDirty, dryRun = false, cwd = repoRoot }) {
  const status = runText("git", ["status", "--porcelain"], { cwd });
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

function currentBranch({ cwd = repoRoot } = {}) {
  try {
    return runText("git", ["symbolic-ref", "--quiet", "--short", "HEAD"], { cwd });
  } catch {
    throw new Error("Creating a release tag requires a named branch; detached HEAD is not supported.");
  }
}

function assertReleaseBranch(branch, { allowNonMain }) {
  if (branch !== "main" && !allowNonMain) {
    throw new Error(`Current branch is '${branch}', not 'main'. Use --allow-non-main to override.`);
  }
}

function tagExists(tag, { cwd = repoRoot } = {}) {
  const result = spawnSync("git", ["rev-parse", "--quiet", "--verify", `refs/tags/${tag}`], {
    cwd,
    encoding: "utf8",
  });
  return result.status === 0;
}

function remoteRefs(refs, { cwd = repoRoot } = {}) {
  const output = runText("git", ["ls-remote", "origin", ...refs], { cwd });
  return new Map(
    output
      .split("\n")
      .filter(Boolean)
      .map((line) => {
        const [commit, ref] = line.split(/\s+/, 2);
        return [ref, commit];
      }),
  );
}

function remoteTagCommit(tag, { cwd = repoRoot } = {}) {
  const tagRef = `refs/tags/${tag}`;
  const refs = remoteRefs([tagRef, `${tagRef}^{}`], { cwd });
  return refs.get(`${tagRef}^{}`) || refs.get(tagRef) || "";
}

function remoteBranchCommit(branch, { cwd = repoRoot } = {}) {
  return remoteRefs([`refs/heads/${branch}`], { cwd }).get(`refs/heads/${branch}`) || "";
}

function assertRemoteTagMissing(tag, { cwd = repoRoot } = {}) {
  if (remoteTagCommit(tag, { cwd })) {
    throw new Error(`Tag already exists on origin: ${tag}`);
  }
}

function cleanupFailedLocalTag(tag, preparedCommit, { cwd = repoRoot } = {}) {
  if (!tagExists(tag, { cwd })) {
    return;
  }
  const localCommit = runText("git", ["rev-parse", "--verify", `${tag}^{commit}`], { cwd });
  const originCommit = remoteTagCommit(tag, { cwd });
  if (localCommit === preparedCommit && originCommit !== preparedCommit) {
    run("git", ["tag", "-d", tag], { cwd });
  }
}

function verifyPublishedRelease(tag, branch, preparedCommit, { cwd = repoRoot } = {}) {
  const localTagCommit = runText("git", ["rev-parse", "--verify", `${tag}^{commit}`], { cwd });
  const originTagCommit = remoteTagCommit(tag, { cwd });
  const originBranchCommit = remoteBranchCommit(branch, { cwd });
  if (
    localTagCommit !== preparedCommit ||
    originTagCommit !== preparedCommit ||
    originBranchCommit !== preparedCommit
  ) {
    throw new Error(
      `Published release verification failed for ${tag}; deployment was withheld. Inspect origin before retrying with --tag.`,
    );
  }
}

export function prepareCreateTag(tag, { allowNonMain = false, dryRun = false, cwd = repoRoot } = {}) {
  validateTag(tag);
  const branch = currentBranch({ cwd });
  assertReleaseBranch(branch, { allowNonMain });
  assertCleanWorktree({ allowDirty: false, cwd });
  if (tagExists(tag, { cwd })) {
    throw new Error(`Tag already exists: ${tag}`);
  }
  assertRemoteTagMissing(tag, { cwd });

  const branchRef = `refs/heads/${branch}`;
  const remoteBranchSnapshot = remoteRefs(["refs/heads/main", branchRef], { cwd });
  const remoteMain = remoteBranchSnapshot.get("refs/heads/main") || "";
  if (!remoteMain) {
    throw new Error("origin/main does not exist; cannot establish the release baseline.");
  }
  const remoteBranchBeforeRebase = remoteBranchSnapshot.get(branchRef) || "";
  const initialBranchLease = `--force-with-lease=${branchRef}:${remoteBranchBeforeRebase}`;

  const fetchArgs = [
    "fetch",
    "--no-tags",
    "origin",
    "+refs/heads/main:refs/remotes/origin/main",
  ];
  if (dryRun) {
    run("git", ["fetch", "--dry-run", "--no-tags", "origin", "refs/heads/main"], {
      cwd,
      quiet: true,
    });
    const head = runText("git", ["rev-parse", "--verify", "HEAD^{commit}"], { cwd });
    const preparedCommit = head === remoteMain ? head : "<post-rebase-commit>";
    const branchRefspec = `${preparedCommit}:refs/heads/${branch}`;
    const tagRefspec = `refs/tags/${tag}:refs/tags/${tag}`;
    run("git", fetchArgs, { cwd, dryRun: true });
    run("git", ["rebase", "--no-autostash", "refs/remotes/origin/main"], {
      cwd,
      dryRun: true,
    });
    run("git", ["push", initialBranchLease, "origin", branchRefspec], {
      cwd,
      dryRun: true,
    });
    run("git", ["tag", "-a", tag, "-m", `Hermes deploy ${tag}`, preparedCommit], {
      cwd,
      dryRun: true,
    });
    const publicationLease = `--force-with-lease=${branchRef}:${preparedCommit}`;
    run("git", ["push", "--atomic", publicationLease, "origin", branchRefspec, tagRefspec], {
      cwd,
      dryRun: true,
    });
    if (preparedCommit === "<post-rebase-commit>") {
      console.log("! The release commit will be known only after rebasing onto the latest origin/main.");
    }
    return { branch, sourceCommit: preparedCommit };
  }

  run("git", fetchArgs, { cwd });
  try {
    run("git", ["rebase", "--no-autostash", "refs/remotes/origin/main"], { cwd });
  } catch (error) {
    try {
      run("git", ["rebase", "--abort"], { cwd, quiet: true });
    } catch {
      // Preserve the original rebase error; Git reports when there is nothing to abort.
    }
    throw new Error(`Rebase onto origin/main failed and the release was stopped:\n${error.message}`);
  }

  if (currentBranch({ cwd }) !== branch) {
    throw new Error("The current branch changed during release preparation.");
  }
  assertCleanWorktree({ allowDirty: false, cwd });
  const preparedCommit = runText("git", ["rev-parse", "--verify", "HEAD^{commit}"], { cwd });
  const localBranchCommit = runText("git", ["rev-parse", "--verify", `refs/heads/${branch}^{commit}`], {
    cwd,
  });
  if (localBranchCommit !== preparedCommit) {
    throw new Error("HEAD no longer matches the prepared release branch.");
  }

  const branchRefspec = `${preparedCommit}:${branchRef}`;
  run("git", ["push", initialBranchLease, "origin", branchRefspec], { cwd });
  if (remoteBranchCommit(branch, { cwd }) !== preparedCommit) {
    throw new Error("The release branch could not be verified on origin; no tag was created.");
  }
  assertRemoteTagMissing(tag, { cwd });

  run("git", ["tag", "-a", tag, "-m", `Hermes deploy ${tag}`, preparedCommit], { cwd });
  const tagRefspec = `refs/tags/${tag}:refs/tags/${tag}`;
  const publicationLease = `--force-with-lease=${branchRef}:${preparedCommit}`;
  try {
    run("git", ["push", "--atomic", publicationLease, "origin", branchRefspec, tagRefspec], {
      cwd,
    });
  } catch (error) {
    const originTagCommit = remoteTagCommit(tag, { cwd });
    const originBranchCommit = remoteBranchCommit(branch, { cwd });
    if (originTagCommit === preparedCommit && originBranchCommit === preparedCommit) {
      console.log("! Atomic push reported an error, but exact remote refs confirm publication succeeded.");
    } else {
      cleanupFailedLocalTag(tag, preparedCommit, { cwd });
      throw error;
    }
  }

  verifyPublishedRelease(tag, branch, preparedCommit, { cwd });
  return { branch, sourceCommit: preparedCommit };
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
    "mv",
    [
      path.join(buildDir, "deploy/powerpoint-runtime/node_modules"),
      path.join(buildDir, "deploy/powerpoint-runtime/runtime-modules"),
    ],
    { dryRun },
  );
  run(
    "tar",
    [
      "-czf",
      archivePath,
      "--no-xattrs",
      "--exclude=._*",
      "--exclude=*/._*",
      "--exclude=./node_modules",
      "--exclude=./web/node_modules",
      "--exclude=./ui-tui/node_modules",
      "--exclude=./apps/*/node_modules",
      "--exclude=./deploy/powerpoint-runtime/runtime-modules/.package-lock.json",
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
  run(
    "npm",
    ["ci", "--omit=dev", "--ignore-scripts", "--no-audit"],
    { dryRun, cwd: path.join(buildDir, "deploy/powerpoint-runtime") },
  );
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
dashboard_public_url="$9"
migrate_nginx_hermes="${"${"}10}"
dashboard_public_host="${"${"}11}"
provision_powerpoint_deps="${"${"}12}"
python_package_index="${"${"}13}"
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
runtimes_dir="$remote_root/runtimes/python"
sandbox_dir="/etc/hermes"
sandbox_policy="$sandbox_dir/executor-sandbox.json"
sandbox_seccomp="$sandbox_dir/executor-x86_64.bpf"
cgroup_root="/sys/fs/cgroup/system.slice/hermes-dashboard.service/authenticated-owners"
owner_root="$hermes_home/users"
service_user="hermes"
service_group="hermes"
old_current_target=""
new_current_target=""
release_target=""
rollback_dir=""
deployment_committed="0"
services_touched="0"
smoke_root=""
powerpoint_smoke_owner=""

gateway_unit="/etc/systemd/system/hermes-gateway.service"
dashboard_unit="/etc/systemd/system/hermes-dashboard.service"

backup_deployment_state() {
  rollback_dir="$(mktemp -d "$tmp_dir/hermes-rollback.XXXXXX")"
  for path in "$gateway_unit" "$dashboard_unit" "$runner" "$sandbox_policy" "$sandbox_seccomp"; do
    if [ -e "$path" ]; then
      cp -a -- "$path" "$rollback_dir/$(printf '%s' "$path" | sed 's#/#_#g')"
    fi
  done
}

restore_deployment_state() {
  local path backup
  for path in "$gateway_unit" "$dashboard_unit" "$runner" "$sandbox_policy" "$sandbox_seccomp"; do
    backup="$rollback_dir/$(printf '%s' "$path" | sed 's#/#_#g')"
    if [ -e "$backup" ]; then
      cp -a -- "$backup" "$path"
    else
      rm -f -- "$path"
    fi
  done
  if [ -n "$old_current_target" ]; then
    ln -sfnT "$old_current_target" "$current"
  else
    rm -f -- "$current"
  fi
}

cleanup_release_tmp() {
  local exit_status="$?"
  if [ "$deployment_committed" != "1" ] && [ -n "$rollback_dir" ]; then
    restore_deployment_state || true
    systemctl daemon-reload || true
    if [ "$services_touched" = "1" ] && [ -n "$old_current_target" ]; then
      systemctl restart hermes-gateway.service || true
      systemctl restart hermes-dashboard.service || true
    fi
  fi
  rm -rf -- "$release_tmp"
  [ -z "$smoke_root" ] || rm -rf -- "$smoke_root"
  [ -z "$powerpoint_smoke_owner" ] || rm -rf -- "$powerpoint_smoke_owner"
  [ -z "$rollback_dir" ] || rm -rf -- "$rollback_dir"
  rm -f -- "$archive"
  rmdir -- "$release_lock" 2>/dev/null || true
  return "$exit_status"
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
if [[ "$dashboard_public_url" != http://* && "$dashboard_public_url" != https://* ]]; then
  echo "Invalid dashboard public URL" >&2
  exit 1
fi
if [[ "$migrate_nginx_hermes" != "0" && "$migrate_nginx_hermes" != "1" ]]; then
  echo "Invalid Nginx migration mode" >&2
  exit 1
fi
if [[ "$provision_powerpoint_deps" != "0" && "$provision_powerpoint_deps" != "1" ]]; then
  echo "Invalid PowerPoint provisioning mode" >&2
  exit 1
fi
if [[ "$python_package_index" != https://* ]]; then
  echo "Invalid Python package index" >&2
  exit 1
fi

for required in tar systemctl sha256sum readlink realpath stat sort mv getent useradd groupadd runuser install cp find ldd sed curl rpm python3; do
  if ! command -v "$required" >/dev/null 2>&1; then
    echo "Missing required command: $required" >&2
    exit 1
  fi
done

if ! getent group "$service_group" >/dev/null; then
  groupadd --system "$service_group"
fi
if ! getent passwd "$service_user" >/dev/null; then
  useradd --system --gid "$service_group" --home-dir "$shared" --shell /usr/sbin/nologin "$service_user"
fi
mkdir -p "$releases_dir" "$tmp_dir" "$hermes_home" "$owner_root" "$runtimes_dir" "$sandbox_dir"
chown -R "$service_user:$service_group" "$hermes_home"
chmod 0750 "$owner_root"
if [ ! -f "$env_file" ]; then
  umask 077
  : > "$env_file"
fi
chown root:"$service_group" "$env_file"
chmod 0640 "$env_file"
chmod 0750 "$hermes_home" 2>/dev/null || true

if ! mkdir -- "$release_lock"; then
  echo "Release is already being deployed or requires investigation: $release_id" >&2
  exit 1
fi
if [ -L "$current" ]; then
  old_current_target="$(resolved_path "$current")"
fi
backup_deployment_state

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
  chown -R root:root "$release_tmp"
  find "$release_tmp" -type d -exec chmod go-w {} +
  find "$release_tmp" -type f -exec chmod go-w {} +
  mv -- "$release_tmp" "$release"
fi
rm -f -- "$archive"

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
export UV_NO_CONFIG=1
export HERMES_HOME="$hermes_home"

test -f "$release/hermes_cli/web_dist/index.html"
test -f "$release/ui-tui/dist/entry.js"
test -f "$release/deploy/powerpoint-runtime/package-lock.json"
test -d "$release/deploy/powerpoint-runtime/runtime-modules/pptxgenjs"
test -f "$release/deploy/runtime/alicloud3-powerpoint-packages.json"
test -f "$release/deploy/smoke-powerpoint-runtime.py"
test -f "$release/deploy/check-executor-cgroup-host.py"
test -f "$release/deploy/smoke-executor-resources.py"
test -f "$release/skills/productivity/powerpoint/scripts/office/soffice.py"

powerpoint_manifest="$release/deploy/runtime/alicloud3-powerpoint-packages.json"
manifest_values() {
  python3 - "$powerpoint_manifest" "$1" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    document = json.load(handle)
key = sys.argv[2]
if key == "packages":
    print(" ".join(item["nevra"] for item in document["packages"]))
elif key == "entries":
    print("\n".join(f'{item["name"]}|{item["nevra"]}' for item in document["packages"]))
else:
    print(document["distribution"][key])
PY
}
expected_distro="$(manifest_values id)"
expected_version="$(manifest_values versionId)"
expected_platform="$(manifest_values platformId)"
expected_architecture="$(manifest_values architecture)"
. /etc/os-release
architecture="$(uname -m)"
if [ "$ID" != "$expected_distro" ] || [ "$VERSION_ID" != "$expected_version" ] || [ "${"${"}PLATFORM_ID:-}" != "$expected_platform" ] || [ "$architecture" != "$expected_architecture" ]; then
  echo "PowerPoint runtime package manifest does not match this host" >&2
  exit 1
fi
powerpoint_packages="$(manifest_values packages)"
powerpoint_package_entries="$(manifest_values entries)"
if [ "$provision_powerpoint_deps" = "1" ]; then
  if ! command -v dnf >/dev/null 2>&1; then
    echo "Missing required command: dnf (needed to provision PowerPoint dependencies)" >&2
    exit 1
  fi
  echo "Provisioning reviewed PowerPoint host prerequisites"
  dnf install -y --setopt=install_weak_deps=False $powerpoint_packages
fi
installed_powerpoint_packages=''
while IFS='|' read -r package expected; do
  [ -n "$package" ] || continue
  installed="$(rpm -q --qf '%{NAME}-%{EPOCHNUM}:%{VERSION}-%{RELEASE}.%{ARCH}' "$package" 2>/dev/null || true)"
  if [ "$installed" != "$expected" ]; then
    echo "Missing or incompatible PowerPoint package: $package${"${"}installed:+ ($installed)}" >&2
    echo "Re-run with --provision-powerpoint-deps for the reviewed additive install" >&2
    exit 1
  fi
  installed_powerpoint_packages="${"${"}installed_powerpoint_packages}${"${"}installed}\n"
done <<<"$powerpoint_package_entries"

lock_hash="$(sha256sum "$release/uv.lock" | cut -d ' ' -f1)"
powerpoint_lock_hash="$(sha256sum "$release/deploy/powerpoint-runtime/package-lock.json" | cut -d ' ' -f1)"
powerpoint_package_hash="$(printf '%b' "$installed_powerpoint_packages" | sort | sha256sum | cut -d ' ' -f1)"
node_path="$(type -P node || true)"
if [ -z "$node_path" ]; then
  echo "Missing required command: node" >&2
  exit 1
fi
node_identity="$(printf '%s\n' "$(node --version)" "$(sha256sum "$node_path" | cut -d ' ' -f1)" | sha256sum | cut -d ' ' -f1)"
python_version="3.11"
runtime_inputs_hash="$(printf '%s\n' "$lock_hash" "$powerpoint_lock_hash" "$powerpoint_package_hash" "$node_identity" 'sandbox9' | sha256sum | cut -d ' ' -f1)"
runtime_id="py311-${"${"}architecture}-${"${"}runtime_inputs_hash}-sandbox9"
venv="$runtimes_dir/$runtime_id"
# One manifest drives both packaging and preflight. Keep it aligned with
# ShellFileOperations' target-side scripts, especially atomic writes. Keep
# /bin/sh explicit because LibreOffice's launcher uses that absolute shebang,
# while this host resolves the sh command from /usr/bin.
executor_commands="bash sh /bin/sh ls pwd printf cat chmod grep find head mktemp mv rm stat awk basename dirname sed uname which node soffice"

if [ ! -x "$venv/bin/python3" ]; then
  echo "Bootstrapping immutable Python runtime $runtime_id"
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
  runtime_tmp="$runtimes_dir/.${"${"}runtime_id}.tmp.$$"
  rm -rf -- "$runtime_tmp"
  mkdir -p "$runtime_tmp/python-base" "$runtime_tmp/venv" "$runtime_tmp/toolchain" "$runtime_tmp/powerpoint"
  cp -a "$release/deploy/powerpoint-runtime/runtime-modules" "$runtime_tmp/powerpoint/node_modules"
  uv python install "$python_version" --install-dir "$runtime_tmp/python-base" --no-bin
  base_python="$(find "$runtime_tmp/python-base" -type f -path '*/bin/python3*' -perm -u+x | sort | head -n 1)"
  if [ -z "$base_python" ]; then
    echo "uv-managed Python executable was not installed" >&2
    exit 1
  fi
  UV_PYTHON_DOWNLOADS=never uv venv --relocatable --python "$base_python" "$runtime_tmp/venv"
  cd "$release"
  UV_PROJECT_ENVIRONMENT="$runtime_tmp/venv" UV_DEFAULT_INDEX="$python_package_index" \
    uv sync --extra all --extra ddgs --locked --no-editable --link-mode copy
  cp -a "$runtime_tmp/venv/." "$runtime_tmp/"
  rm -rf -- "$runtime_tmp/venv"
  python_target="$(readlink "$runtime_tmp/bin/python3" || true)"
  if [ -n "$python_target" ]; then
    case "$python_target" in
      /*) echo "Sandbox Python points outside the runtime" >&2; exit 1 ;;
    esac
  fi
  resolved_python="$(readlink -f "$runtime_tmp/bin/python3")"
  case "$resolved_python" in
    "$runtime_tmp"/*) ;;
    *) echo "Sandbox Python resolves outside the runtime" >&2; exit 1 ;;
  esac
  resolved_python="$(readlink -f "$runtime_tmp/bin/python3")"
  while read -r library; do
    [ -n "$library" ] || continue
    library_target="$runtime_tmp/toolchain$library"
    mkdir -p "$(dirname "$library_target")"
    cp -aL -- "$library" "$library_target"
  done < <(ldd "$resolved_python" | sed -nE 's#.*=> (/[^ ]+).*#\1#p; s#^[[:space:]]*(/[^ ]+).*#\1#p')
  while IFS= read -r -d '' extension; do
    while read -r library; do
      [ -n "$library" ] || continue
      library_target="$runtime_tmp/toolchain$library"
      mkdir -p "$(dirname "$library_target")"
      cp -aL -- "$library" "$library_target"
    done < <(ldd "$extension" | sed -nE 's#.*=> (/[^ ]+).*#\1#p; s#^[[:space:]]*(/[^ ]+).*#\1#p')
  done < <(find "$runtime_tmp/lib/python3.11/site-packages" -type f -name '*.so' -print0)
  for command in $executor_commands; do
    [ "$command" != "soffice" ] || continue
    case "$command" in
      /*) command_path="$command" ;;
      *) command_path="$(type -P "$command" || true)" ;;
    esac
    if [ ! -x "$command_path" ]; then
      echo "Missing local executor command: $command" >&2
      exit 1
    fi
    command_target="$runtime_tmp/toolchain$command_path"
    mkdir -p "$(dirname "$command_target")"
    cp -aL -- "$command_path" "$command_target"
    while read -r library; do
      [ -n "$library" ] || continue
      library_target="$runtime_tmp/toolchain$library"
      mkdir -p "$(dirname "$library_target")"
      cp -aL -- "$library" "$library_target"
    done < <(ldd "$command_path" | sed -nE 's#.*=> (/[^ ]+).*#\1#p; s#^[[:space:]]*(/[^ ]+).*#\1#p')
  done
  while IFS= read -r package; do
    [ -n "$package" ] || continue
    while IFS= read -r packaged_path; do
      case "$packaged_path" in
        /usr/bin/*|/usr/lib/*|/usr/lib64/*|/usr/share/*|/etc/fonts|/etc/fonts/*)
          if [ -e "$packaged_path" ] && [ ! -d "$packaged_path" ]; then
            package_target="$runtime_tmp/toolchain$packaged_path"
            mkdir -p "$(dirname "$package_target")"
            cp -aL -- "$packaged_path" "$package_target"
          fi
          ;;
        /etc/X11/fontpath.d/*)
          if [ ! -L "$packaged_path" ]; then
            echo "PowerPoint package owns an unexpected non-symlink X11 font path: $packaged_path" >&2
            exit 1
          fi
          fontpath_target="$(readlink "$packaged_path")"
          case "$fontpath_target" in
            /usr/share/fonts/*) ;;
            *)
              echo "PowerPoint package X11 font path has an unexpected target: $packaged_path -> $fontpath_target" >&2
              exit 1
              ;;
          esac
          ;;
        /etc/*|/bin/*|/lib/*|/lib64/*)
          echo "PowerPoint package owns an unexpected protected path: $packaged_path" >&2
          exit 1
          ;;
      esac
    done < <(rpm -ql "$package")
  done < <(printf '%s\n' "$powerpoint_package_entries" | cut -d'|' -f1)
  soffice_source="$(type -P soffice || true)"
  if [ "$soffice_source" != "/usr/bin/soffice" ] || [ ! -L "$soffice_source" ]; then
    echo "Host soffice launcher is not the reviewed /usr/bin/soffice symlink" >&2
    exit 1
  fi
  soffice_link="$(readlink "$soffice_source")"
  if [ "$soffice_link" != "/usr/lib64/libreoffice/program/soffice" ]; then
    echo "Host soffice launcher target is unexpected: $soffice_link" >&2
    exit 1
  fi
  soffice_target="$runtime_tmp/toolchain/usr/bin/soffice"
  soffice_launcher="$runtime_tmp/toolchain/usr/lib64/libreoffice/program/soffice"
  if [ ! -f "$soffice_launcher" ]; then
    echo "Packaged soffice launcher is unavailable" >&2
    exit 1
  fi
  rm -f -- "$soffice_target"
  ln -s ../lib64/libreoffice/program/soffice "$soffice_target"
  while IFS= read -r -d '' executable; do
    while read -r library; do
      [ -n "$library" ] || continue
      library_target="$runtime_tmp/toolchain$library"
      mkdir -p "$(dirname "$library_target")"
      cp -aL -- "$library" "$library_target"
    done < <(ldd "$executable" 2>/dev/null | sed -nE 's#.*=> (/[^ ]+).*#\1#p; s#^[[:space:]]*(/[^ ]+).*#\1#p')
  done < <(find "$runtime_tmp/toolchain/usr/lib64/libreoffice" -type f -print0 2>/dev/null)
  chown -R root:root "$runtime_tmp"
  find "$runtime_tmp" -type d -exec chmod 0755 {} +
  find "$runtime_tmp" -type f -exec chmod go-w {} +
  find "$runtime_tmp" -type f ! -perm -u+x -exec chmod 0644 {} +
  mv -- "$runtime_tmp" "$venv"
  final_python="$(find "$venv/python-base" -type f -path '*/bin/python3*' -perm -u+x | sort | head -n 1)"
  if [ -z "$final_python" ]; then
    echo "Final sandbox Python executable was not installed" >&2
    exit 1
  fi
  final_python_relative="$(realpath --relative-to="$venv/bin" "$final_python")"
  ln -sfn "$final_python_relative" "$venv/bin/python"
else
  echo "Reusing immutable Python runtime $venv"
fi

if [ ! -x /usr/bin/bwrap ]; then
  echo "Bubblewrap must be installed at /usr/bin/bwrap" >&2
  exit 1
fi
bwrap_help="$(/usr/bin/bwrap --help 2>&1)"
for option in --bind-fd --ro-bind-fd --size --uid --gid --cap-drop --seccomp --remount-ro --info-fd; do
  if ! grep -F -- "$option" <<<"$bwrap_help" >/dev/null; then
    echo "Bubblewrap lacks required option: $option" >&2
    exit 1
  fi
done

test -f "$release/deploy/sandbox/executor-x86_64.bpf"
seccomp_digest="$(sha256sum "$release/deploy/sandbox/executor-x86_64.bpf" | cut -d ' ' -f1)"
install -o root -g root -m 0444 "$release/deploy/sandbox/executor-x86_64.bpf" "$sandbox_seccomp"
image_digest="$(printf '%s:%s' "$source_commit" "$runtime_id" | sha256sum | cut -d ' ' -f1)"
readonly_mounts=''
for destination in /bin /usr/bin /lib /lib64 /usr/lib /usr/lib64 /usr/share /etc/fonts; do
  source="$venv/toolchain$destination"
  [ -d "$source" ] || continue
  readonly_mounts="$readonly_mounts,{\"source\":\"$source\",\"destination\":\"$destination\"}"
done
# Policy loading stays available before the host migration so chat can start and
# tools can fail closed. The trusted Dashboard bootstrap creates the exact
# delegated cgroup v2 directory only after systemd has created its service scope.
policy_tmp="$sandbox_policy.tmp.$$"
cat > "$policy_tmp" <<POLICY
{"schema_version":2,"architecture":"$architecture","owner_root":"$owner_root","uid":$(id -u "$service_user"),"gid":$(getent group "$service_group" | cut -d: -f3),"bwrap_binary":"/usr/bin/bwrap","release_root":"$release","runtime_root":"$venv","python_executable":"/opt/hermes/python/bin/python3","readonly_mounts":[{"source":"$release","destination":"/opt/hermes/release"},{"source":"$venv","destination":"/opt/hermes/python"}$readonly_mounts],"syscall_policy_id":"executor-local-v1","syscall_policy_digest":"sha256:$seccomp_digest","seccomp_artifact":"$sandbox_seccomp","image_digest":"sha256:$image_digest","profile":"executor-bwrap-v1","security_backend":"host-bwrap-seccomp-v1","network_mode":"isolated-tool-network","verifier":"host-sandbox-policy-v1","record_ttl_seconds":30,"root_tmpfs_bytes":67108864,"executor_tmpfs_bytes":33554432,"allowed_egress_profiles":["tool-none"],"resource_policy":{"cgroup_root":"$cgroup_root","required_controllers":["cpu","memory","pids"],"global":{"cpu_millis":1500,"memory_bytes":2415919104,"pids":512,"max_concurrent_executors":2,"max_owner_workers":5},"owner":{"cpu_millis":1000,"memory_bytes":939524096,"pids":128,"max_concurrent_executors":1},"executor":{"cpu_millis":750,"memory_bytes":536870912,"pids":64,"max_concurrent_executors":1,"swap_bytes":0,"file_descriptors":64,"duration_seconds":120,"output_bytes":200000},"cleanup_grace_seconds":2,"cleanup_timeout_seconds":10,"cgroup_kill_required":false}}
POLICY
chown root:root "$policy_tmp"
chmod 0644 "$policy_tmp"
mv -- "$policy_tmp" "$sandbox_policy"

for command in $executor_commands; do
  case "$command" in
    /*) test -x "$venv/toolchain$command" ;;
    *) PATH="$venv/toolchain/usr/bin:$venv/toolchain/bin" command -v "$command" >/dev/null ;;
  esac
done
PYTHONPATH="$release" "$venv/bin/python" -c 'import hermes_cli.tool_executor_runtime.entrypoint, tools.registry'

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
venv="${"${"}VIRTUAL_ENV:?VIRTUAL_ENV is required}"

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
User=$service_user
Group=$service_group
Environment=HERMES_REMOTE_ROOT=$remote_root
Environment=HERMES_HOME=$hermes_home
Environment=HERMES_ENV_FILE=$env_file
Environment=VIRTUAL_ENV=$venv
Environment=HERMES_SANDBOX_DEPLOYMENT_POLICY=hermes_cli.owner_worker.host_sandbox:host_sandbox_deployment_policy
Environment=HERMES_DISABLE_LAZY_INSTALLS=1
WorkingDirectory=$current
# Gateway does not execute authenticated tools. Resource governance is admitted
# by Dashboard/Owner Worker and may fail closed without making Gateway unavailable.
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
User=$service_user
Group=$service_group
Environment=HERMES_REMOTE_ROOT=$remote_root
Environment=HERMES_HOME=$hermes_home
Environment=HERMES_ENV_FILE=$env_file
Environment=VIRTUAL_ENV=$venv
Environment=HERMES_DASHBOARD_PUBLIC_URL=$dashboard_public_url
Environment=HERMES_SANDBOX_DEPLOYMENT_POLICY=hermes_cli.owner_worker.host_sandbox:host_sandbox_deployment_policy
Environment=HERMES_DISABLE_LAZY_INSTALLS=1
WorkingDirectory=$current
ExecStart=$venv/bin/python -m hermes_cli.owner_worker.cgroup_bootstrap --managed-root $cgroup_root -- $runner dashboard --host 127.0.0.1 --port 9119 --no-open --skip-build --require-auth --trust-proxy-headers
Restart=always
RestartSec=5
Delegate=cpu memory pids
CPUAccounting=yes
MemoryAccounting=yes
TasksAccounting=yes
# Keep owner workers in the dashboard service cgroup so shutdown cleanup can
# revoke their authority fence before systemd reaps any remaining children.
KillMode=control-group
KillSignal=SIGTERM
TimeoutStopSec=60
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable hermes-gateway.service hermes-dashboard.service
services_touched="1"
# Stop the control plane before rotating a release, then terminate any worker
# that survived an older dashboard unit. Older KillMode=mixed releases could
# leave those children orphaned with an ACTIVE durable lease, blocking every
# cold start after the new dashboard comes up.
systemctl stop hermes-dashboard.service
owner_worker_pids="$(pgrep -f '[h]ermes_cli.owner_worker.entrypoint' || true)"
if [ -n "$owner_worker_pids" ]; then
  kill -TERM $owner_worker_pids || true
  for _ in $(seq 1 50); do
    live_owner_workers=""
    for pid in $owner_worker_pids; do
      if kill -0 "$pid" 2>/dev/null; then
        live_owner_workers="$live_owner_workers $pid"
      fi
    done
    [ -z "$live_owner_workers" ] && break
    sleep 0.1
  done
  [ -z "${"${"}live_owner_workers:-}" ] || kill -KILL $live_owner_workers || true
fi
if ! systemctl restart hermes-gateway.service || ! systemctl start hermes-dashboard.service || \
   ! systemctl is-active --quiet hermes-gateway.service || \
   ! systemctl is-active --quiet hermes-dashboard.service; then
  echo "New services failed; restoring previous deployment state" >&2
  restore_deployment_state
  deployment_committed="1"
  systemctl daemon-reload
  if [ -n "$old_current_target" ]; then
    systemctl restart hermes-gateway.service || true
    systemctl restart hermes-dashboard.service || true
  fi
  exit 1
fi
systemctl --no-pager --full status hermes-gateway.service hermes-dashboard.service || true

if "$venv/bin/python" "$release/deploy/check-executor-cgroup-host.py" \
  --managed-root "$cgroup_root" \
  --service hermes-dashboard.service \
  --require-ready; then
  echo "HERMES_DEPLOY_STAGE executor_resource_preflight=passed"
  PYTHONPATH="$release" "$venv/bin/python" -c 'from hermes_cli.owner_worker.host_sandbox import host_sandbox_deployment_policy; host_sandbox_deployment_policy()'
  "$venv/bin/python" "$release/deploy/smoke-executor-resources.py" \
    --managed-root "$cgroup_root" \
    --timeout 10
  echo "HERMES_DEPLOY_STAGE executor_resource_smoke=passed"
  powerpoint_smoke_owner="$owner_root/.deploy-powerpoint-smoke.$$"
  if ! runuser -u "$service_user" -- env -i \
    HOME="$shared" \
    PATH="$venv/bin:/usr/bin:/bin" \
    PYTHONPATH="$release" \
    PYTHONNOUSERSITE=1 \
    "$venv/bin/python" "$release/deploy/smoke-powerpoint-runtime.py" \
    --owner-home "$powerpoint_smoke_owner" \
    --policy "$sandbox_policy" \
    --timeout 45; then
    echo "PowerPoint runtime smoke failed" >&2
    rm -rf -- "$powerpoint_smoke_owner"
    exit 1
  fi
  rm -rf -- "$powerpoint_smoke_owner"
  powerpoint_smoke_owner=""
  echo "HERMES_DEPLOY_STAGE powerpoint_runtime_smoke=passed"
else
  echo "HERMES_DEPLOY_STAGE executor_resource_preflight=unavailable"
  echo "Authenticated tools remain fail closed until the documented cgroup v2 migration is complete"
fi

# Prove Hermes' own gate is active before touching the legacy outer Nginx gate.
# systemd can report active before Uvicorn has opened its socket, so retry the
# exact fail-closed contract for up to 30 seconds rather than racing startup.
login_status="000"
api_status="000"
for _ in $(seq 1 30); do
  login_status="$(curl -sS -o /dev/null -w '%{http_code}' \
    -H "Host: $dashboard_public_host" \
    -H "X-Forwarded-Host: $dashboard_public_host" \
    -H 'X-Forwarded-Proto: https' \
    -H 'X-Forwarded-Prefix: /hermes' \
    http://127.0.0.1:9119/ || true)"
  api_status="$(curl -sS -o /dev/null -w '%{http_code}' \
    -H "Host: $dashboard_public_host" \
    -H "X-Forwarded-Host: $dashboard_public_host" \
    -H 'X-Forwarded-Proto: https' \
    -H 'X-Forwarded-Prefix: /hermes' \
    http://127.0.0.1:9119/api/sessions || true)"
  if [ "$login_status" = "302" ] && [ "$api_status" = "401" ]; then
    break
  fi
  sleep 1
done
if [ "$login_status" != "302" ] || [ "$api_status" != "401" ]; then
  echo "Hermes internal auth preflight failed (html=$login_status api=$api_status)" >&2
  exit 1
fi

# Gate the transaction with a real gateway conversation while the previous
# deployment is still restorable. The runner receives no production env file or
# model credentials and enforces loopback-only network access itself.
smoke_root="$(mktemp -d "$tmp_dir/hermes-conversation-smoke.XXXXXX")"
chown "$service_user:$service_group" "$smoke_root"
chmod 0700 "$smoke_root"
echo "Running deterministic conversation smoke before deployment commit"
if ! (
  cd "$smoke_root"
  exec runuser -u "$service_user" -- env -i \
    HOME="$smoke_root" \
    TMPDIR="$smoke_root" \
    PATH="$venv/bin:/usr/local/bin:/usr/bin:/bin" \
    PYTHONPATH="$release" \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    "$venv/bin/python" "$release/deploy/smoke-conversation.py" --timeout 90
); then
  echo "HERMES_DEPLOY_STAGE deterministic_smoke=failed" >&2
  echo "Deterministic conversation smoke failed; deployment remains uncommitted and will be rolled back" >&2
  exit 1
fi
echo "HERMES_DEPLOY_STAGE deterministic_smoke=passed"
rm -rf -- "$smoke_root"
smoke_root=""

action="reconcile"
[ "$migrate_nginx_hermes" = "1" ] && action="migrate"
"$venv/bin/python" "$release/deploy/nginx/manage_hermes_proxy.py" \
  "$action" \
  --vhost /etc/nginx/conf.d/abinllm.conf \
  --snippet-source "$release/deploy/nginx/hermes-dashboard.conf" \
  --snippet-target /etc/nginx/snippets/hermes-dashboard.conf

deployment_committed="1"
echo "HERMES_DEPLOY_STAGE deployment=committed"
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
  return runSsh(
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
      args.dashboardPublicUrl,
      args.migrateNginxHermes ? "1" : "0",
      args.dashboardPublicHost,
      args.provisionPowerpointDeps ? "1" : "0",
      DEFAULT_PYTHON_PACKAGE_INDEX,
    ],
    { input: remoteDeployScript() },
  );
}

function runPublicConversationSmoke(args) {
  const commandArgs = [
    path.join(repoRoot, "scripts", "smoke_dashboard_conversation.py"),
    "--url",
    args.dashboardPublicUrl,
    "--timeout",
    "180",
  ];
  try {
    run("python3", commandArgs, { dryRun: args.dryRun });
    return args.dryRun ? "planned" : "passed";
  } catch (error) {
    console.error(`Public dashboard conversation smoke failed: ${error.message}`);
    return "failed";
  }
}

function remoteStagePassed(error, stage) {
  const output = [error?.commandResult?.stdout, error?.commandResult?.stderr]
    .filter(Boolean)
    .join("\n");
  return output.includes(`HERMES_DEPLOY_STAGE ${stage}=passed`);
}

function printSummary(args, result) {
  const remoteRoot = args.remoteRoot.replace(/\/+$/, "");
  const target = `${args.user}@${args.host}`;
  console.log(`\nRelease validation summary`);
  console.log(`Deploy target: ${target}:${remoteRoot}`);
  console.log(`${args.sourceKind === "commit" ? "Commit SHA" : "Tag"}: ${args.sourceKind === "commit" ? args.sourceCommit : args.sourceTag}`);
  console.log(`Current symlink: ${remoteRoot}/current -> ${remoteRoot}/releases/${args.releaseId}`);
  console.log(`State dir: ${remoteRoot}/shared/.hermes`);
  console.log(`Env file: ${remoteRoot}/shared/.env`);
  console.log(`Services: hermes-gateway.service, hermes-dashboard.service`);
  console.log(`Dashboard: ${args.dashboardPublicUrl} (Hermes user login only)`);
  console.log(
    `Nginx: ${args.migrateNginxHermes ? "explicit legacy-block migration" : "managed snippet reconciliation"}`,
  );
  console.log(`PowerPoint runtime smoke: ${result.powerpointSmoke}`);
  console.log(`PowerPoint host provisioning: ${args.provisionPowerpointDeps ? "enabled" : "preflight only"}`);
  console.log(`Deterministic conversation smoke: ${result.deterministicSmoke}`);
  console.log(`Public real-AI conversation smoke: ${result.publicSmoke}`);
  console.log(`Release outcome: ${result.outcome}`);
  console.log("Remote staging archive and deterministic smoke state are removed after use.");
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
    const prepared = prepareCreateTag(args.createTag, {
      allowNonMain: args.allowNonMain,
      dryRun: args.dryRun,
    });
    args.sourceTag = args.createTag;
    args.sourceCommit = prepared.sourceCommit;
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
  let deploymentCommitted = false;
  try {
    try {
      const remoteResult = deployArchive(args, archivePath);
      deploymentCommitted = args.dryRun || remoteResult.stdout.includes("HERMES_DEPLOY_STAGE deployment=committed");
      if (!deploymentCommitted) {
        throw new Error("remote deployment completed without a commit marker");
      }
    } catch (error) {
      printSummary(args, {
        powerpointSmoke: args.dryRun
          ? "planned"
          : remoteStagePassed(error, "powerpoint_runtime_smoke")
            ? "passed"
            : "failed or not reached",
        deterministicSmoke: "failed or not reached",
        publicSmoke: "not run",
        outcome: "rolled back before commit",
      });
      throw error;
    }

    const publicSmoke = runPublicConversationSmoke(args);
    const outcome = args.dryRun
      ? "dry-run: deployment and both smoke layers planned"
      : publicSmoke === "passed"
        ? "deployment committed and all smoke passed"
        : "deployment committed but public smoke failed";
    printSummary(args, {
      powerpointSmoke: args.dryRun ? "planned" : "passed",
      deterministicSmoke: args.dryRun ? "planned" : "passed",
      publicSmoke,
      outcome,
    });
    if (publicSmoke === "failed") {
      throw new Error("deployment committed but public smoke failed; automatic rollback was not attempted");
    }
  } finally {
    if (tmp && !args.dryRun) {
      rmSync(tmp, { recursive: true, force: true });
    }
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    main();
  } catch (error) {
    console.error(`deploy failed: ${error.message}`);
    process.exit(1);
  }
}
