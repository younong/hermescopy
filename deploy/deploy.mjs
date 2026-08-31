#!/usr/bin/env node

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { homedir, tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  formatCommand,
  moveDirectory,
  requireFile,
  requireLocalCommand,
  resolvePythonCommand,
  runLocal,
  runLocalText,
} from "./local-platform.mjs";
import { createReleaseArchiveFile, extractSourceArchive } from "./archive.mjs";
import { remoteCommand, runPasswordSsh, uploadPasswordFile } from "./ssh-transport.mjs";

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
const DEFAULT_KEEP_RELEASES = 5;
const DEFAULT_DASHBOARD_PUBLIC_URL = "https://abinllm.xyz/hermes";
const DEPLOY_NPM_WORKSPACES = ["web"];

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(__dirname, "..");

function usage() {
  console.log(`Hermes tag-based bare-metal deploy tool

Usage:
  npm run deploy -- --create-tag v2026.7.3
  npm run deploy -- --tag v2026.7.3
  npm run deploy -- --tag v2026.7.3 --dry-run

Options:
  --tag <tag>              Retry or roll back an existing tag published on origin.
  --create-tag <tag>       Create and deploy one tag from synchronized main.
  --host <host>            SSH host. Default: ${DEFAULT_HOST}
  --user <user>            SSH user. Default: ${DEFAULT_USER}
  --port <port>            SSH port. Default: 22
  --identity-file <path>   SSH private key path. Default: ~/.ssh/hermes_apiyi_ed25519
  --remote-root <path>     Remote release root. Default: ${DEFAULT_REMOTE_ROOT}
  --allow-non-main         Emergency only: create a tag from a named non-main branch.
  --allow-dirty            Allow deploying an existing tag with a dirty worktree.
  --force                  Deprecated and rejected; immutable releases are never replaced.
  --keep-releases <n>      Keep the newest n remote releases after deploy. Default: ${DEFAULT_KEEP_RELEASES}
  --no-prune-releases      Do not delete old remote release directories.
  --no-prune-runtimes      Do not delete unused immutable Python runtimes.
  --dashboard-public-url <url>
                           Public dashboard URL used by the trusted loopback proxy.
                           Default: ${DEFAULT_DASHBOARD_PUBLIC_URL}
  --migrate-nginx-hermes   Explicitly replace the recognized legacy Hermes Nginx
                           auth block after the new internal auth gate is healthy.
  --provision-powerpoint-deps
                           Add reviewed LibreOffice/font host prerequisites before
                           building the immutable PowerPoint executor runtime.
  --initial-continuity-transition
                           Allow the one-time upgrade from a release that cannot yet
                           emit planned-restart 1012 or participate in continuity smoke.
  --check-connection       Verify SSH host key, authentication, and remote Bash only.
                           Does not inspect Git, build, upload, or change remote state.
  --dry-run                Print commands without changing local or remote state.
  -h, --help               Show this help.

Authentication:
  Prefer SSH keys; the key path uses your system OpenSSH client. For temporary
  password authentication, set HERMES_DEPLOY_PASSWORD. The built-in SSH/SFTP
  transport supports native Windows and never prints the password.

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
    pruneRuntimes: true,
    dashboardPublicUrl:
      process.env.HERMES_DEPLOY_DASHBOARD_PUBLIC_URL || DEFAULT_DASHBOARD_PUBLIC_URL,
    migrateNginxHermes: false,
    provisionPowerpointDeps: false,
    initialContinuityTransition: false,
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
      case "--keep-releases":
        args.keepReleases = parsePositiveInteger(next(), arg);
        break;
      case "--no-prune-releases":
        args.pruneReleases = false;
        break;
      case "--no-prune-runtimes":
        args.pruneRuntimes = false;
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
      case "--initial-continuity-transition":
        args.initialContinuityTransition = true;
        break;
      case "--check-connection":
        args.checkConnection = true;
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

  const sourceCount = [args.tag, args.createTag].filter(Boolean).length;
  if (!args.help && !args.checkConnection && sourceCount !== 1) {
    throw new Error("Pass exactly one of --tag or --create-tag.");
  }
  if (!args.help && args.allowNonMain && !args.createTag) {
    throw new Error("--allow-non-main is only valid with --create-tag.");
  }
  if (args.checkConnection && sourceCount !== 0) {
    throw new Error("--check-connection cannot be combined with --tag or --create-tag.");
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

function run(command, commandArgs, options = {}) {
  return runLocal(command, commandArgs, { cwd: repoRoot, ...options });
}

function runText(command, commandArgs, options = {}) {
  return runLocalText(command, commandArgs, { cwd: repoRoot, ...options });
}

function requireBinary(name) {
  requireLocalCommand(name);
}

function validateTag(tag) {
  if (!TAG_RE.test(tag)) {
    throw new Error(`Invalid tag '${tag}'. Use letters, numbers, dots, underscores, and dashes only.`);
  }
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
    throw new Error(
      `Current branch is '${branch}', not 'main'. Merge the change through a PR, then release from synchronized main.`,
    );
  }
}

function tagExists(tag, { cwd = repoRoot } = {}) {
  try {
    run("git", ["rev-parse", "--quiet", "--verify", `refs/tags/${tag}`], {
      cwd,
      quiet: true,
    });
    return true;
  } catch {
    return false;
  }
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

function verifyPublishedTag(tag, preparedCommit, { cwd = repoRoot } = {}) {
  const localTagCommit = runText("git", ["rev-parse", "--verify", `${tag}^{commit}`], { cwd });
  const originTagCommit = remoteTagCommit(tag, { cwd });
  if (localTagCommit !== preparedCommit || originTagCommit !== preparedCommit) {
    throw new Error(
      `Published tag verification failed for ${tag}; deployment was withheld. Inspect origin before retrying with --tag.`,
    );
  }
}

function verifyPublishedEmergencyRelease(tag, branch, preparedCommit, { cwd = repoRoot } = {}) {
  verifyPublishedTag(tag, preparedCommit, { cwd });
  if (remoteBranchCommit(branch, { cwd }) !== preparedCommit) {
    throw new Error(
      `Published emergency release verification failed for ${tag}; deployment was withheld. Inspect origin before retrying with --tag.`,
    );
  }
}

function isAncestor(ancestor, descendant, { cwd = repoRoot } = {}) {
  try {
    run("git", ["merge-base", "--is-ancestor", ancestor, descendant], { cwd, quiet: true });
    return true;
  } catch (error) {
    if (error.commandResult?.status === 1) {
      return false;
    }
    throw new Error(
      error.commandResult?.stderr || "Unable to compare the local and remote main histories.",
      { cause: error },
    );
  }
}

function assertMainSynchronized(localMain, remoteMain, { cwd = repoRoot } = {}) {
  if (localMain === remoteMain) {
    return;
  }
  const localShort = localMain.slice(0, 12);
  const remoteShort = remoteMain.slice(0, 12);
  if (isAncestor(localMain, remoteMain, { cwd })) {
    throw new Error(
      `Local main (${localShort}) is behind origin/main (${remoteShort}). Synchronize local main before releasing.`,
    );
  }
  if (isAncestor(remoteMain, localMain, { cwd })) {
    throw new Error(
      `Local main (${localShort}) contains commits not merged into origin/main (${remoteShort}). Merge them through a PR, then synchronize main before releasing.`,
    );
  }
  throw new Error(
    `Local main (${localShort}) has diverged from origin/main (${remoteShort}). Reconcile the histories through the normal PR workflow before releasing.`,
  );
}

function prepareMainCreateTag(tag, { dryRun, cwd }) {
  const fetchArgs = [
    "fetch",
    "--no-tags",
    "origin",
    "+refs/heads/main:refs/remotes/origin/main",
  ];
  let remoteMain;
  if (dryRun) {
    run("git", ["fetch", "--dry-run", "--no-tags", "origin", "refs/heads/main"], {
      cwd,
      quiet: true,
    });
    remoteMain = remoteBranchCommit("main", { cwd });
  } else {
    run("git", fetchArgs, { cwd });
    remoteMain = runText("git", ["rev-parse", "--verify", "refs/remotes/origin/main^{commit}"], {
      cwd,
    });
  }
  if (!remoteMain) {
    throw new Error("origin/main does not exist; cannot establish the release baseline.");
  }
  if (currentBranch({ cwd }) !== "main") {
    throw new Error("The current branch changed during release preparation.");
  }
  assertCleanWorktree({ allowDirty: false, cwd });
  const preparedCommit = runText("git", ["rev-parse", "--verify", "HEAD^{commit}"], { cwd });
  const localMain = runText("git", ["rev-parse", "--verify", "refs/heads/main^{commit}"], { cwd });
  if (preparedCommit !== localMain) {
    throw new Error("HEAD no longer matches local main.");
  }
  assertMainSynchronized(localMain, remoteMain, { cwd });
  assertRemoteTagMissing(tag, { cwd });

  const tagRefspec = `refs/tags/${tag}:refs/tags/${tag}`;
  if (dryRun) {
    run("git", fetchArgs, { cwd, dryRun: true });
    run("git", ["tag", "-a", tag, "-m", `Hermes deploy ${tag}`, preparedCommit], {
      cwd,
      dryRun: true,
    });
    run("git", ["push", "origin", tagRefspec], { cwd, dryRun: true });
    return { branch: "main", sourceCommit: preparedCommit };
  }

  run("git", ["tag", "-a", tag, "-m", `Hermes deploy ${tag}`, preparedCommit], { cwd });
  try {
    run("git", ["push", "origin", tagRefspec], { cwd });
  } catch (error) {
    if (remoteTagCommit(tag, { cwd }) === preparedCommit) {
      console.log("! Tag push reported an error, but the exact remote tag confirms publication succeeded.");
    } else {
      cleanupFailedLocalTag(tag, preparedCommit, { cwd });
      throw error;
    }
  }
  verifyPublishedTag(tag, preparedCommit, { cwd });
  return { branch: "main", sourceCommit: preparedCommit };
}

function prepareEmergencyNonMainCreateTag(tag, branch, { dryRun, cwd }) {
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
    const branchRefspec = `${preparedCommit}:${branchRef}`;
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
      console.log("! The emergency release commit will be known only after rebasing onto origin/main.");
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
    throw new Error(`Rebase onto origin/main failed and the emergency release was stopped:\n${error.message}`);
  }

  if (currentBranch({ cwd }) !== branch) {
    throw new Error("The current branch changed during emergency release preparation.");
  }
  assertCleanWorktree({ allowDirty: false, cwd });
  const preparedCommit = runText("git", ["rev-parse", "--verify", "HEAD^{commit}"], { cwd });
  const localBranchCommit = runText("git", ["rev-parse", "--verify", `refs/heads/${branch}^{commit}`], {
    cwd,
  });
  if (localBranchCommit !== preparedCommit) {
    throw new Error("HEAD no longer matches the prepared emergency release branch.");
  }

  const branchRefspec = `${preparedCommit}:${branchRef}`;
  run("git", ["push", initialBranchLease, "origin", branchRefspec], { cwd });
  if (remoteBranchCommit(branch, { cwd }) !== preparedCommit) {
    throw new Error("The emergency release branch could not be verified on origin; no tag was created.");
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

  verifyPublishedEmergencyRelease(tag, branch, preparedCommit, { cwd });
  return { branch, sourceCommit: preparedCommit };
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

  return branch === "main"
    ? prepareMainCreateTag(tag, { dryRun, cwd })
    : prepareEmergencyNonMainCreateTag(tag, branch, { dryRun, cwd });
}

export function createReleaseArchive(
  buildDir,
  archivePath,
  { dryRun = false, gitModes = new Map() } = {},
) {
  if (dryRun) {
    console.log(`[dry-run] create release archive ${archivePath}`);
    return;
  }
  const archiveBytes = createReleaseArchiveFile(buildDir, archivePath, { gitModes });
  console.log(`Release archive: ${archiveBytes} bytes (${(archiveBytes / 1024 / 1024).toFixed(2)} MiB)`);
}

function gitTreeModes(sourceCommit, { cwd = repoRoot } = {}) {
  const output = runText("git", ["ls-tree", "-rz", "--full-tree", sourceCommit], { cwd });
  const modes = new Map();
  for (const record of output.split("\0")) {
    if (!record) continue;
    const match = record.match(/^(\d+)\s+\w+\s+[0-9a-f]+\t(.+)$/s);
    if (match) modes.set(match[2].replaceAll("\\", "/"), match[1]);
  }
  return modes;
}

export function releaseManifest({ releaseId, sourceCommit, sourceTag }) {
  if (!sourceTag || releaseId !== sourceTag) {
    throw new Error("Tag release ID must match the source tag.");
  }
  if (!sourceCommit) {
    throw new Error("Tag release manifest requires a source commit.");
  }
  return {
    schemaVersion: 1,
    releaseId,
    source: { kind: "tag", commit: sourceCommit, tag: sourceTag },
  };
}

export function createArchive(args, { dryRun, cwd = repoRoot }) {
  const { releaseId, sourceCommit, sourceTag } = args;
  const tmp = dryRun ? null : mkdtempSync(path.join(tmpdir(), "hermes-deploy-"));
  const buildDir = dryRun ? path.join(tmpdir(), `hermes-${releaseId}-artifact`) : path.join(tmp, "artifact");
  const archivePath = dryRun ? path.join(tmpdir(), `hermes-${releaseId}.tar.gz`) : path.join(tmp, `hermes-${releaseId}.tar.gz`);
  const sourceArchive = dryRun ? path.join(tmpdir(), `hermes-${releaseId}.tar`) : path.join(tmp, `hermes-${releaseId}.tar`);

  if (!dryRun) {
    mkdirSync(buildDir, { recursive: true });
  }
  const archiveEnv = { COPYFILE_DISABLE: "1" };
  run("git", ["archive", "--format=tar", "--output", sourceArchive, sourceCommit], {
    dryRun,
    env: archiveEnv,
    cwd,
  });
  const gitModes = dryRun ? new Map() : gitTreeModes(sourceCommit, { cwd });
  if (!dryRun) {
    extractSourceArchive(sourceArchive, buildDir);
    writeFileSync(
      path.join(buildDir, ".hermes-release.json"),
      `${JSON.stringify(releaseManifest({ releaseId, sourceCommit, sourceTag }), null, 2)}\n`,
      "utf8",
    );
  }

  buildArtifact(buildDir, { dryRun });
  const runtimeModulesSource = path.join(buildDir, "deploy/powerpoint-runtime/node_modules");
  const runtimeModulesTarget = path.join(buildDir, "deploy/powerpoint-runtime/runtime-modules");
  if (dryRun) {
    console.log(`[dry-run] move ${runtimeModulesSource} ${runtimeModulesTarget}`);
  } else {
    moveDirectory(runtimeModulesSource, runtimeModulesTarget);
  }
  createReleaseArchive(buildDir, archivePath, { dryRun, gitModes });
  return { tmp, archivePath };
}

function buildArtifact(buildDir, { dryRun }) {
  const webOutDir = path.join(buildDir, "hermes_cli/web_dist");
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
  run(
    "npm",
    ["ci", "--omit=dev", "--ignore-scripts", "--no-audit"],
    { dryRun, cwd: path.join(buildDir, "deploy/powerpoint-runtime") },
  );
  const webEntry = path.join(buildDir, "hermes_cli/web_dist/index.html");
  if (dryRun) {
    console.log(`[dry-run] verify web build ${webEntry}`);
  } else {
    requireFile(webEntry, "Web build entry point");
  }
}

function sshBaseArgs(args) {
  const base = [
    "-p",
    args.port,
    "-o",
    "BatchMode=yes",
    "-o",
    "PreferredAuthentications=publickey",
    ...SSH_CONNECTION_ARGS,
  ];
  if (args.identityFile) {
    base.push("-i", args.identityFile);
  }
  return base;
}

function scpBaseArgs(args) {
  const base = [
    "-P",
    args.port,
    "-o",
    "BatchMode=yes",
    "-o",
    "PreferredAuthentications=publickey",
    ...SSH_CONNECTION_ARGS,
  ];
  if (args.identityFile) {
    base.push("-i", args.identityFile);
  }
  return base;
}

function remoteTarget(args) {
  return `${args.user}@${args.host}`;
}

async function runSsh(args, remoteArgs, options = {}) {
  if (args.dryRun && options.input) {
    console.log(`[dry-run] remote script:\n${options.input}`);
  }
  if (process.env.HERMES_DEPLOY_PASSWORD) {
    if (args.dryRun) {
      console.log("[dry-run] password SSH transport (password hidden)");
      console.log(`[dry-run] remote ${formatCommand(remoteArgs[0], remoteArgs.slice(1))}`);
      return { stdout: "", stderr: "", status: 0 };
    }
    return runPasswordSsh(args, remoteArgs, { input: options.input });
  }
  const sshArgs = [
    ...sshBaseArgs(args),
    remoteTarget(args),
    // sshd re-joins command argv with spaces before the remote shell parses it,
    // so send one pre-quoted command string instead of raw argv (see #339).
    remoteCommand(remoteArgs[0], remoteArgs.slice(1)),
  ];
  return run("ssh", sshArgs, { dryRun: args.dryRun, input: options.input });
}

async function runScp(args, localPath, remotePath) {
  if (process.env.HERMES_DEPLOY_PASSWORD) {
    if (args.dryRun) {
      console.log(`[dry-run] password SFTP upload ${localPath} ${remotePath} (password hidden)`);
      return { stdout: "", stderr: "", status: 0 };
    }
    return uploadPasswordFile(args, localPath, remotePath);
  }
  const scpArgs = [...scpBaseArgs(args), localPath, `${remoteTarget(args)}:${remotePath}`];
  return run("scp", scpArgs, { dryRun: args.dryRun });
}

function remoteDeployScript() {
  return String.raw`set -euo pipefail
remote_root="$1"
release_id="$2"
source_commit="$3"
source_tag="$4"
archive="$5"
keep_releases="$6"
prune_releases="$7"
dashboard_public_url="$8"
migrate_nginx_hermes="$9"
dashboard_public_host="${"${"}10}"
provision_powerpoint_deps="${"${"}11}"
python_package_index="${"${"}12}"
prune_runtimes="${"${"}13}"
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
staged_runner="$tmp_dir/hermes-service-runner.$$.sh"
staged_dashboard_unit="$tmp_dir/hermes-dashboard.$$.service"
staged_sandbox_policy="$tmp_dir/executor-sandbox.$$.json"
staged_sandbox_seccomp="$tmp_dir/executor-x86_64.$$.bpf"
cgroup_root="/sys/fs/cgroup/system.slice/hermes-dashboard.service/authenticated-owners"
owner_root="$hermes_home/users"
service_user="hermes"
service_group="hermes"
owner_worker_drain_timeout=120
owner_worker_runtime_limit=4
dashboard_soft_nofile=65536
dashboard_hard_nofile=1048576
dashboard_stop_timeout="$((owner_worker_drain_timeout + 30))"
old_current_target=""
new_current_target=""
release_target=""
rollback_dir=""
deployment_committed="0"
services_touched="0"
legacy_gateway_was_enabled="0"
legacy_gateway_was_active="0"
smoke_root=""
authority_smoke_root=""
authority_smoke_result=""
reader_smoke_root=""
reader_smoke_result=""
conversation_smoke_result=""
powerpoint_smoke_owner=""
authority_snapshot=""
runtime_pruning_status="disabled"

gateway_unit="/etc/systemd/system/hermes-gateway.service"
dashboard_unit="/etc/systemd/system/hermes-dashboard.service"
dashboard_dropin_dir="/etc/systemd/system/hermes-dashboard.service.d"
nginx_log_format="/etc/nginx/conf.d/00-hermes-log-format.conf"
legacy_nginx_log_format="/etc/nginx/conf.d/hermes-log-format.conf"

backup_deployment_state() {
  rollback_dir="$(mktemp -d "$tmp_dir/hermes-rollback.XXXXXX")"
  chown root:"$service_group" "$rollback_dir"
  chmod 0710 "$rollback_dir"
  systemctl is-enabled --quiet hermes-gateway.service && legacy_gateway_was_enabled="1" || true
  systemctl is-active --quiet hermes-gateway.service && legacy_gateway_was_active="1" || true
  for path in "$gateway_unit" "$dashboard_unit" "$runner" "$sandbox_policy" "$sandbox_seccomp" "$nginx_log_format" "$legacy_nginx_log_format"; do
    if [ -e "$path" ]; then
      cp -a -- "$path" "$rollback_dir/$(printf '%s' "$path" | sed 's#/#_#g')"
    fi
  done
  if [ -d "$dashboard_dropin_dir" ]; then
    cp -a -- "$dashboard_dropin_dir" "$rollback_dir/$(printf '%s' "$dashboard_dropin_dir" | sed 's#/#_#g')"
  fi
}

restore_deployment_state() {
  local path backup dashboard_dropin_backup
  for path in "$gateway_unit" "$dashboard_unit" "$runner" "$sandbox_policy" "$sandbox_seccomp" "$nginx_log_format" "$legacy_nginx_log_format"; do
    backup="$rollback_dir/$(printf '%s' "$path" | sed 's#/#_#g')"
    if [ -e "$backup" ]; then
      cp -a -- "$backup" "$path"
    else
      rm -f -- "$path"
    fi
  done
  dashboard_dropin_backup="$rollback_dir/$(printf '%s' "$dashboard_dropin_dir" | sed 's#/#_#g')"
  if [ -e "$dashboard_dropin_backup" ]; then
    rm -rf -- "$dashboard_dropin_dir"
    cp -a -- "$dashboard_dropin_backup" "$dashboard_dropin_dir"
  else
    rm -rf -- "$dashboard_dropin_dir"
  fi
  if [ -n "$old_current_target" ]; then
    rollback_link="$current.rollback.$$"
    ln -sT "$old_current_target" "$rollback_link"
    mv -Tf "$rollback_link" "$current"
  else
    rm -f -- "$current"
  fi
}

snapshot_authority() {
  local authority_source="$hermes_home/control-plane/authority.sqlite3"
  if [ ! -f "$authority_source" ]; then
    authority_snapshot=""
    return 0
  fi
  authority_snapshot="$rollback_dir/authority/authority.sqlite3"
  install -d -o "$service_user" -g "$service_group" -m 0700 "$(dirname "$authority_snapshot")"
  runuser -u "$service_user" -- env -i \
    HOME="$shared" HERMES_HOME="$hermes_home" \
    "$venv/bin/python" - "$authority_source" "$authority_snapshot" <<'PY'
import os
import sqlite3
import sys
from pathlib import Path

source_path = Path(sys.argv[1])
destination = Path(sys.argv[2])
source = sqlite3.connect(
    f"file:{source_path.resolve().as_posix()}?mode=ro",
    uri=True,
    timeout=30,
)
target = sqlite3.connect(destination, timeout=30)
try:
    source.backup(target)
    integrity = target.execute("PRAGMA integrity_check").fetchone()
    if not integrity or str(integrity[0]).lower() != "ok":
        raise RuntimeError("authority snapshot integrity check failed")
finally:
    target.close()
    source.close()
os.chmod(destination, 0o600)
PY
  chown -R root:"$service_group" "$(dirname "$authority_snapshot")"
  chmod 0710 "$(dirname "$authority_snapshot")"
  chmod 0640 "$authority_snapshot"
}

restore_authority_snapshot() {
  [ -n "$authority_snapshot" ] && [ -f "$authority_snapshot" ] || return 0
  local authority_target="$hermes_home/control-plane/authority.sqlite3"
  runuser -u "$service_user" -- env -i \
    HOME="$shared" HERMES_HOME="$hermes_home" \
    "$venv/bin/python" - "$authority_snapshot" "$authority_target" <<'PY'
import os
import sqlite3
import sys
from pathlib import Path

source_path = Path(sys.argv[1])
target_path = Path(sys.argv[2])
restore_path = Path(str(target_path) + ".rollback")
for path in (restore_path, Path(str(restore_path) + "-wal"), Path(str(restore_path) + "-shm")):
    try:
        path.unlink()
    except FileNotFoundError:
        pass
source = sqlite3.connect(
    f"file:{source_path.resolve().as_posix()}?mode=ro&immutable=1",
    uri=True,
    timeout=30,
)
target = sqlite3.connect(restore_path, timeout=30)
try:
    source.backup(target)
    integrity = target.execute("PRAGMA integrity_check").fetchone()
    if not integrity or str(integrity[0]).lower() != "ok":
        raise RuntimeError("restored authority integrity check failed")
finally:
    target.close()
    source.close()
os.chmod(restore_path, 0o600)
os.replace(restore_path, target_path)
for suffix in ("-wal", "-shm"):
    sidecar = Path(str(target_path) + suffix)
    try:
        sidecar.unlink()
    except FileNotFoundError:
        pass
PY
}

cleanup_release_tmp() {
  local exit_status="$?"
  if [ "$deployment_committed" != "1" ] && [ -n "$rollback_dir" ]; then
    if [ "$services_touched" = "1" ]; then
      systemctl stop hermes-dashboard.service hermes-gateway.service || true
      restore_authority_snapshot || true
    fi
    restore_deployment_state || true
    systemctl daemon-reload || true
    if [ "$services_touched" = "1" ] && [ -n "$old_current_target" ]; then
      if [ "$legacy_gateway_was_enabled" = "1" ]; then
        systemctl enable hermes-gateway.service || true
      fi
      if [ "$legacy_gateway_was_active" = "1" ]; then
        systemctl start hermes-gateway.service || true
      fi
      systemctl start hermes-dashboard.service || true
    fi
  fi
  rm -rf -- "$release_tmp"
  [ -z "$smoke_root" ] || rm -rf -- "$smoke_root"
  [ -z "$authority_smoke_root" ] || rm -rf -- "$authority_smoke_root"
  [ -z "$reader_smoke_root" ] || rm -rf -- "$reader_smoke_root"
  [ -z "$powerpoint_smoke_owner" ] || rm -rf -- "$powerpoint_smoke_owner"
  rm -f -- "$staged_runner" "$staged_dashboard_unit" "$staged_sandbox_policy" "$staged_sandbox_seccomp" "$current.next.$$" "$current.rollback.$$"
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

prune_unused_runtimes() {
  if [ "$prune_runtimes" != "1" ]; then
    echo "Runtime pruning disabled; keeping all directories under $runtimes_dir"
    return
  fi
  if "$venv/bin/python" "$release/deploy/prune-unused-runtimes.py" \
    --runtimes-dir "$runtimes_dir" \
    --keep-runtime "$venv"; then
    runtime_pruning_status="completed"
  else
    runtime_pruning_status="failed (deployment remains committed)"
    echo "Runtime pruning failed after deployment commit; inspect runtimes manually" >&2
  fi
}

if ! is_release_name "$release_id"; then
  echo "Invalid release ID on remote: $release_id" >&2
  exit 1
fi
if ! [[ "$source_commit" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Invalid immutable release commit" >&2
  exit 1
fi
if ! is_release_name "$source_tag" || [ "$release_id" != "$source_tag" ]; then
  echo "Invalid release tag source" >&2
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
if [[ "$prune_runtimes" != "0" && "$prune_runtimes" != "1" ]]; then
  echo "Invalid runtime pruning mode" >&2
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

expected_manifest="{\"schemaVersion\":1,\"releaseId\":\"$release_id\",\"source\":{\"kind\":\"tag\",\"commit\":\"$source_commit\",\"tag\":\"$source_tag\"}}"
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
test -f "$release/deploy/powerpoint-runtime/package-lock.json"
test -d "$release/deploy/powerpoint-runtime/runtime-modules/pptxgenjs"
test -f "$release/deploy/runtime/alicloud3-powerpoint-packages.json"
test -f "$release/deploy/smoke-powerpoint-runtime.py"
test -f "$release/deploy/check-executor-cgroup-host.py"
test -f "$release/deploy/smoke-executor-resources.py"
test -f "$release/deploy/run-cgroup-smoke.py"
test -f "$release/deploy/smoke-authority-concurrency.py"
test -f "$release/deploy/smoke-session-reader.py"
test -f "$release/deploy/smoke-conversation.py"
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
pyproject_hash="$(sha256sum "$release/pyproject.toml" | cut -d ' ' -f1)"
powerpoint_lock_hash="$(sha256sum "$release/deploy/powerpoint-runtime/package-lock.json" | cut -d ' ' -f1)"
powerpoint_package_hash="$(printf '%b' "$installed_powerpoint_packages" | sort | sha256sum | cut -d ' ' -f1)"
node_path="$(type -P node || true)"
if [ -z "$node_path" ]; then
  echo "Missing required command: node" >&2
  exit 1
fi
node_identity="$(printf '%s\n' "$(node --version)" "$(sha256sum "$node_path" | cut -d ' ' -f1)" | sha256sum | cut -d ' ' -f1)"
python_version="3.11"
runtime_dependency_profile="all,anthropic,ddgs,voice"
runtime_inputs_hash="$(printf '%s\n' "$lock_hash" "$pyproject_hash" "$powerpoint_lock_hash" "$powerpoint_package_hash" "$node_identity" "$runtime_dependency_profile" 'sandbox10' | sha256sum | cut -d ' ' -f1)"
runtime_id="py311-${"${"}architecture}-${"${"}runtime_inputs_hash}-sandbox10"
venv="$runtimes_dir/$runtime_id"
# One manifest drives both packaging and preflight. Keep it aligned with
# ShellFileOperations' target-side scripts, especially atomic writes. Keep
# /bin/sh explicit because LibreOffice's launcher uses that absolute shebang,
# while this host resolves the sh command from /usr/bin.
executor_commands="bash sh /bin/sh ls pwd printf cat chmod grep find head mktemp mv rm stat awk basename dirname sed uname which node soffice"

if [ ! -x "$venv/bin/python3" ]; then
  echo "Bootstrapping immutable Python runtime $runtime_id"
  runtime_build_started=$SECONDS
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
    uv sync --extra all --extra anthropic --extra ddgs --extra voice --locked --no-editable --link-mode copy
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
  dependency_reference_count=0
  dependency_unique_count=0
  dependency_duplicate_count=0
  dependency_copied_count=0
  dependency_existing_count=0
  libreoffice_candidate_count=0
  declare -A runtime_dependency_seen=()

  copy_runtime_dependency() {
    local library="$1"
    local library_target
    dependency_reference_count=$((dependency_reference_count + 1))
    if [ -n "${"${"}runtime_dependency_seen[$library]+x}" ]; then
      dependency_duplicate_count=$((dependency_duplicate_count + 1))
      return
    fi
    runtime_dependency_seen["$library"]=1
    dependency_unique_count=$((dependency_unique_count + 1))
    library_target="$runtime_tmp/toolchain$library"
    if [ -e "$library_target" ] || [ -L "$library_target" ]; then
      if [ ! -f "$library_target" ] || [ -L "$library_target" ]; then
        echo "Runtime dependency target is not a regular file: $library_target" >&2
        exit 1
      fi
      dependency_existing_count=$((dependency_existing_count + 1))
      return
    fi
    mkdir -p "$(dirname "$library_target")"
    cp -aL -- "$library" "$library_target"
    dependency_copied_count=$((dependency_copied_count + 1))
  }

  collect_runtime_dependencies() {
    local dependency_source="$1"
    local library
    while IFS= read -r library; do
      [ -n "$library" ] || continue
      copy_runtime_dependency "$library"
    done < <(ldd "$dependency_source" 2>/dev/null | sed -nE 's#.*=> (/[^ ]+).*#\1#p; s#^[[:space:]]*(/[^ ]+).*#\1#p')
  }

  pre_rpm_started=$SECONDS
  collect_runtime_dependencies "$resolved_python"
  while IFS= read -r -d '' extension; do
    collect_runtime_dependencies "$extension"
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
    collect_runtime_dependencies "$command_path"
  done
  pre_rpm_seconds=$((SECONDS - pre_rpm_started))
  rpm_copy_started=$SECONDS
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
  rpm_copy_seconds=$((SECONDS - rpm_copy_started))
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
  libreoffice_started=$SECONDS
  while IFS= read -r -d '' executable; do
    libreoffice_candidate_count=$((libreoffice_candidate_count + 1))
    collect_runtime_dependencies "$executable"
  done < <(
    find "$runtime_tmp/toolchain/usr/lib64/libreoffice" \
      -type f \( -name '*.so*' -o -perm /111 \) -print0 2>/dev/null
  )
  libreoffice_seconds=$((SECONDS - libreoffice_started))
  runtime_build_seconds=$((SECONDS - runtime_build_started))
  if [ "$dependency_reference_count" -ne $((dependency_unique_count + dependency_duplicate_count)) ] || \
     [ "$dependency_unique_count" -ne $((dependency_copied_count + dependency_existing_count)) ]; then
    echo "Runtime dependency collection counters are inconsistent" >&2
    exit 1
  fi
  echo "HERMES_DEPLOY_RUNTIME_BUILD runtime_id=$runtime_id pre_rpm_seconds=$pre_rpm_seconds rpm_seconds=$rpm_copy_seconds libreoffice_seconds=$libreoffice_seconds libreoffice_candidates=$libreoffice_candidate_count dependency_references=$dependency_reference_count dependency_unique=$dependency_unique_count dependency_duplicates=$dependency_duplicate_count dependency_copied=$dependency_copied_count dependency_existing=$dependency_existing_count total_seconds=$runtime_build_seconds"
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
install -o root -g root -m 0444 "$release/deploy/sandbox/executor-x86_64.bpf" "$staged_sandbox_seccomp"
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
policy_tmp="$staged_sandbox_policy"
cat > "$policy_tmp" <<POLICY
{"schema_version":2,"architecture":"$architecture","owner_root":"$owner_root","uid":$(id -u "$service_user"),"gid":$(getent group "$service_group" | cut -d: -f3),"bwrap_binary":"/usr/bin/bwrap","release_root":"$release","runtime_root":"$venv","python_executable":"/opt/hermes/python/bin/python3","readonly_mounts":[{"source":"$release","destination":"/opt/hermes/release"},{"source":"$venv","destination":"/opt/hermes/python"}$readonly_mounts],"syscall_policy_id":"executor-local-v1","syscall_policy_digest":"sha256:$seccomp_digest","seccomp_artifact":"$sandbox_seccomp","image_digest":"sha256:$image_digest","profile":"executor-bwrap-v1","security_backend":"host-bwrap-seccomp-v1","network_mode":"isolated-tool-network","verifier":"host-sandbox-policy-v1","record_ttl_seconds":30,"root_tmpfs_bytes":67108864,"executor_tmpfs_bytes":33554432,"allowed_egress_profiles":["tool-none"],"resource_policy":{"cgroup_root":"$cgroup_root","required_controllers":["cpu","memory","pids"],"global":{"cpu_millis":1500,"memory_bytes":2415919104,"pids":512,"max_concurrent_executors":2,"max_owner_workers":5},"owner":{"cpu_millis":1000,"memory_bytes":939524096,"pids":128,"max_concurrent_executors":1},"reader":{"cpu_millis":1000,"memory_bytes":134217728,"pids":16,"max_concurrent_executors":1},"executor":{"cpu_millis":750,"memory_bytes":536870912,"pids":64,"max_concurrent_executors":1,"swap_bytes":0,"file_descriptors":64,"duration_seconds":300,"output_bytes":200000},"cleanup_grace_seconds":2,"cleanup_timeout_seconds":10,"cgroup_kill_required":false}}
POLICY
chown root:root "$policy_tmp"
chmod 0644 "$policy_tmp"

for command in $executor_commands; do
  case "$command" in
    /*) test -x "$venv/toolchain$command" ;;
    *) PATH="$venv/toolchain/usr/bin:$venv/toolchain/bin" command -v "$command" >/dev/null ;;
  esac
done
PYTHONPATH="$release" "$venv/bin/python" -c 'import anthropic, faster_whisper, hermes_cli.tool_executor_runtime.entrypoint, pilk, tools.registry, tools.silk_decoder'

release_target="$(resolved_path "$release")"
new_current_target="$release_target"

cat > "$staged_runner" <<'RUNNER'
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
chmod 0755 "$staged_runner"

cat > "$staged_dashboard_unit" <<UNIT
[Unit]
Description=Hermes Dashboard
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
Environment=MALLOC_ARENA_MAX=2
Environment=HERMES_DASHBOARD_PUBLIC_URL=$dashboard_public_url
Environment=HERMES_SANDBOX_DEPLOYMENT_POLICY=hermes_cli.owner_worker.host_sandbox:host_sandbox_deployment_policy
Environment=HERMES_DISABLE_LAZY_INSTALLS=1
Environment=HERMES_OWNER_WORKER_MAX=$owner_worker_runtime_limit
Environment=HERMES_OWNER_WORKER_DRAIN_TIMEOUT=$owner_worker_drain_timeout
WorkingDirectory=$current
ExecStart=$venv/bin/python -m hermes_cli.owner_worker.cgroup_bootstrap --managed-root $cgroup_root -- $runner dashboard --host 127.0.0.1 --port 9119 --no-open --skip-build --trust-proxy-headers
Restart=always
RestartSec=5
Delegate=cpu memory pids
CPUAccounting=yes
MemoryAccounting=yes
TasksAccounting=yes
LimitNOFILE=$dashboard_soft_nofile:$dashboard_hard_nofile
# Signal only the Dashboard first so its shutdown hook can drain owner workers;
# systemd still SIGKILLs any processes left in the cgroup after the stop timeout.
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=$dashboard_stop_timeout
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

# Candidate-release authority preflight runs before any service or active
# artifact changes. It is strictly read-only and refuses recovery-required or
# unreadable state; operators must use the offline authority workflow instead.
if ! authority_status="$(
  runuser -u "$service_user" -- env -i \
    HOME="$shared" HERMES_HOME="$hermes_home" PYTHONPATH="$release" \
    "$venv/bin/python" -m hermes_cli.main dashboard authority status --json
)"; then
  echo "HERMES_DEPLOY_STAGE authority_preflight=failed" >&2
  echo "Authority preflight failed. Restart cannot recover authority; offline recovery fencing is required. Run 'hermes dashboard authority status'." >&2
  exit 1
fi
printf '%s' "$authority_status" | "$venv/bin/python" -c '
import json, sys
payload = json.load(sys.stdin)
if payload.get("state") not in {"healthy", "uninitialized"}:
    raise SystemExit(
        "Restart cannot recover authority; offline recovery fencing is required."
    )
'
echo "HERMES_DEPLOY_STAGE authority_preflight=passed"

# Stop the old release before changing any active artifact. Dashboard shutdown
# drains and revokes its Owner Workers. Load the candidate stop policy before
# stopping so this transition is governed by the same drain budget that will be
# used for the candidate service; a stale unit must never turn a graceful stop
# into an immediate SIGKILL.
services_touched="1"
snapshot_authority
install -o root -g root -m 0644 "$staged_dashboard_unit" "$dashboard_unit"
if [ -d "$dashboard_dropin_dir" ]; then
  echo "Removing legacy Dashboard systemd drop-in directory: $dashboard_dropin_dir"
  rm -rf -- "$dashboard_dropin_dir"
fi
systemctl daemon-reload
expected_stop_minutes="$((dashboard_stop_timeout / 60))"
expected_stop_seconds="$((dashboard_stop_timeout % 60))"
expected_stop_usec="$((dashboard_stop_timeout * 1000000))"
effective_stop_timeout="$(systemctl show hermes-dashboard.service -p TimeoutStopUSec --value)"
case "$effective_stop_timeout" in
  "${"${"}dashboard_stop_timeout}s"|"${"${"}expected_stop_usec}us"|"${"${"}expected_stop_minutes}min ${"${"}expected_stop_seconds}s") ;;
  *)
    echo "Dashboard stop timeout is not the configured drain budget (expected=${"${"}dashboard_stop_timeout}s actual=${"${"}effective_stop_timeout})" >&2
    exit 1
    ;;
esac
systemctl stop hermes-dashboard.service
if systemctl list-unit-files hermes-gateway.service --no-legend 2>/dev/null | grep -q '^hermes-gateway.service'; then
  systemctl stop hermes-gateway.service || true
  systemctl disable hermes-gateway.service || true
fi
if systemctl is-active --quiet hermes-dashboard.service || systemctl is-active --quiet hermes-gateway.service; then
  echo "Old services did not stop before release switch" >&2
  exit 1
fi
rm -f -- "$gateway_unit"
install -o root -g root -m 0755 "$staged_runner" "$runner"
install -o root -g root -m 0644 "$staged_sandbox_policy" "$sandbox_policy"
install -o root -g root -m 0444 "$staged_sandbox_seccomp" "$sandbox_seccomp"
next_current="$current.next.$$"
ln -sT "$release" "$next_current"
mv -Tf "$next_current" "$current"
systemctl daemon-reload
systemctl enable hermes-dashboard.service
if ! systemctl start hermes-dashboard.service || \
   ! systemctl is-active --quiet hermes-dashboard.service; then
  echo "New service failed; deployment remains uncommitted and will be rolled back" >&2
  exit 1
fi
systemctl --no-pager --full status hermes-dashboard.service || true

# systemd reports active as soon as the Dashboard process starts, before app
# construction has initialized the managed cgroup hierarchy. Wait for Uvicorn's
# exact unauthenticated contract so resource smoke cannot race CgroupV2Manager.
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

if ! executor_host_status="$(
  "$venv/bin/python" "$release/deploy/check-executor-cgroup-host.py" \
    --managed-root "$cgroup_root" \
    --service hermes-dashboard.service \
    --expected-soft-nofile "$dashboard_soft_nofile" \
    --expected-hard-nofile "$dashboard_hard_nofile" \
    --require-mandatory
)"; then
  echo "Executor host preflight could not inspect the candidate service" >&2
  exit 1
fi
printf '%s\n' "$executor_host_status"
if ! printf '%s' "$executor_host_status" | "$venv/bin/python" -c '
import json, sys
payload = json.load(sys.stdin)
raise SystemExit(0 if payload.get("mandatoryReady") is True else 1)
'; then
  echo "Dashboard LimitNOFILE verification failed; refusing deployment" >&2
  exit 1
fi
if printf '%s' "$executor_host_status" | "$venv/bin/python" -c '
import json, sys
payload = json.load(sys.stdin)
raise SystemExit(0 if payload.get("resourceReady") is True else 1)
'; then
  echo "HERMES_DEPLOY_STAGE executor_resource_preflight=passed"
  PYTHONPATH="$release" "$venv/bin/python" -c 'from hermes_cli.owner_worker.host_sandbox import host_sandbox_deployment_policy; host_sandbox_deployment_policy()'
  "$venv/bin/python" "$release/deploy/smoke-executor-resources.py" \
    --managed-root "$cgroup_root" \
    --timeout 10
  echo "HERMES_DEPLOY_STAGE executor_resource_smoke=passed"
  powerpoint_smoke_owner="$owner_root/.deploy-powerpoint-smoke.$$"
  if powerpoint_smoke_result="$(
    env -i \
      HOME="$shared" \
      PATH="$venv/bin:/usr/bin:/bin" \
      PYTHONPATH="$release" \
      PYTHONNOUSERSITE=1 \
      "$venv/bin/python" "$release/deploy/run-cgroup-smoke.py" \
      --managed-root "$cgroup_root" \
      --service hermes-dashboard.service \
      --user "$service_user" \
      -- \
      "$venv/bin/python" "$release/deploy/smoke-powerpoint-runtime.py" \
      --owner-home "$powerpoint_smoke_owner" \
      --policy "$sandbox_policy" \
      --timeout 45
  )"; then
    printf '%s\n' "$powerpoint_smoke_result"
  else
    printf '%s\n' "$powerpoint_smoke_result" >&2
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

# Exercise the candidate authority implementation with isolated synthetic state.
# This deliberately never points HOME, TMPDIR, or HERMES_HOME at shared state.
authority_smoke_root="$(mktemp -d "$tmp_dir/hermes-authority-release-smoke.XXXXXX")"
chown "$service_user:$service_group" "$authority_smoke_root"
chmod 0700 "$authority_smoke_root"
echo "Running deterministic authority concurrency smoke before deployment commit"
if ! authority_smoke_result="$(
  cd "$authority_smoke_root"
  exec runuser -u "$service_user" -- env -i \
    HOME="$authority_smoke_root" \
    TMPDIR="$authority_smoke_root" \
    HERMES_HOME="$authority_smoke_root" \
    PATH="$venv/bin:/usr/local/bin:/usr/bin:/bin" \
    PYTHONPATH="$release" \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    "$venv/bin/python" "$release/deploy/smoke-authority-concurrency.py" \
    --root "$authority_smoke_root/work"
)"; then
  printf '%s\n' "$authority_smoke_result"
  echo "HERMES_DEPLOY_STAGE authority_concurrency_smoke=failed" >&2
  echo "Authority concurrency smoke failed; deployment remains uncommitted and will be rolled back" >&2
  exit 1
fi
printf '%s\n' "$authority_smoke_result"
if ! printf '%s' "$authority_smoke_result" | "$venv/bin/python" -c '
import json, sys
result = json.load(sys.stdin)
required = {
    "environment_isolation", "concurrent_initialization", "scope_visibility",
    "browser_exact_once", "worker_bootstrap_exact_once", "worker_lifecycle",
    "authority_checkpoint", "authority_integrity", "authority_schema",
    "authority_recovery_state", "recovery_artifacts", "artifact_cleanup",
}
checks = {
    item.get("name") for item in result.get("checks", [])
    if isinstance(item, dict) and item.get("status") == "passed"
}
cleanup = result.get("cleanup") or {}
observations = result.get("observations") or {}
if result.get("schemaVersion") != 1:
    raise SystemExit("unsupported authority concurrency smoke schema")
if result.get("kind") != "hermes.authority-concurrency-smoke":
    raise SystemExit("unexpected authority concurrency smoke kind")
if result.get("status") != "passed" or not required.issubset(checks):
    raise SystemExit("authority concurrency smoke did not pass all required checks")
if (
    observations.get("checkpoint", {}).get("busy") != 0
    or observations.get("integrity") != "ok"
    or observations.get("schemaVersion") != 10
    or observations.get("recoveryRequired") != 0
    or observations.get("recoveryArtifacts") != 0
):
    raise SystemExit("authority concurrency smoke observations are invalid")
if not cleanup or not all(bool(value) for value in cleanup.values()):
    raise SystemExit("authority concurrency smoke cleanup was incomplete")
'; then
  echo "HERMES_DEPLOY_STAGE authority_concurrency_smoke=failed" >&2
  echo "Authority concurrency smoke result validation failed; deployment remains uncommitted and will be rolled back" >&2
  exit 1
fi
echo "HERMES_DEPLOY_STAGE authority_concurrency_smoke=passed"
rm -rf -- "$authority_smoke_root"
authority_smoke_root=""

# Gate Reader performance before commit using only isolated synthetic state.
reader_smoke_root="$(mktemp -d "$tmp_dir/hermes-reader-release-smoke.XXXXXX")"
chown "$service_user:$service_group" "$reader_smoke_root"
chmod 0700 "$reader_smoke_root"
echo "Running deterministic Session Reader performance smoke before deployment commit"
if ! reader_smoke_result="$(
  cd "$reader_smoke_root"
  exec runuser -u "$service_user" -- env -i \
    HOME="$reader_smoke_root" \
    TMPDIR="$reader_smoke_root" \
    PATH="$venv/bin:/usr/local/bin:/usr/bin:/bin" \
    PYTHONPATH="$release" \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    "$venv/bin/python" "$release/deploy/smoke-session-reader.py" \
    --root "$reader_smoke_root/work"
)"; then
  printf '%s\n' "$reader_smoke_result"
  echo "HERMES_DEPLOY_STAGE session_reader_performance_smoke=failed" >&2
  echo "Session Reader performance smoke failed; deployment remains uncommitted and will be rolled back" >&2
  exit 1
fi
printf '%s\n' "$reader_smoke_result"
printf '%s' "$reader_smoke_result" | "$venv/bin/python" -c '
import json, sys
result = json.load(sys.stdin)
required = {
    "reader_resource_contract", "reader_fixture", "reader_query_plan",
    "list_sql_budget", "stats_sql_budget", "search_sql_budget",
    "local_compact_listing", "reader_startup", "reader_uds_cold",
    "reader_uds_warm",
}
checks = {item.get("name") for item in result.get("checks", []) if isinstance(item, dict)}
cleanup = result.get("cleanup") or {}
if result.get("schemaVersion") != 1:
    raise SystemExit("unsupported Session Reader smoke schema")
if result.get("kind") != "hermes.session-reader-performance-smoke":
    raise SystemExit("unexpected Session Reader smoke kind")
if result.get("status") != "passed" or not required.issubset(checks):
    raise SystemExit("Session Reader smoke did not pass all required checks")
if not all(bool(value) for value in cleanup.values()):
    raise SystemExit("Session Reader smoke cleanup was incomplete")
'
echo "HERMES_DEPLOY_STAGE session_reader_performance_smoke=passed"
rm -rf -- "$reader_smoke_root"
reader_smoke_root=""

# Gate the transaction with a real gateway conversation while the previous
# deployment is still restorable. The runner receives no production env file or
# model credentials and enforces loopback-only network access itself.
smoke_root="$(mktemp -d /opt/hcs-XXXXXX)"
chown "$service_user:$service_group" "$smoke_root"
chmod 0700 "$smoke_root"
echo "Running deterministic conversation smoke before deployment commit"
if ! conversation_smoke_result="$(
  env -i \
    HOME="$smoke_root" \
    TMPDIR="$smoke_root" \
    PATH="$venv/bin:/usr/local/bin:/usr/bin:/bin" \
    PYTHONPATH="$release" \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    "$venv/bin/python" "$release/deploy/run-cgroup-smoke.py" \
      --managed-root "$cgroup_root" \
      --service hermes-dashboard.service \
      --user "$service_user" \
      -- \
      "$venv/bin/python" "$release/deploy/smoke-conversation.py" \
        --timeout 90 \
        --root "$smoke_root" \
        --sandbox-policy hermes_cli.owner_worker.host_sandbox:isolated_smoke_sandbox_deployment_policy
)"; then
  printf '%s\n' "$conversation_smoke_result"
  echo "HERMES_DEPLOY_STAGE deterministic_smoke=failed" >&2
  echo "Deterministic conversation smoke failed; deployment remains uncommitted and will be rolled back" >&2
  exit 1
fi
printf '%s\n' "$conversation_smoke_result"
echo "HERMES_DEPLOY_STAGE deterministic_smoke=passed"
rm -rf -- "$smoke_root"
smoke_root=""

install -o root -g root -m 0644 \
  "$release/deploy/nginx/hermes-log-format.conf" \
  "$nginx_log_format"
rm -f -- "$legacy_nginx_log_format"
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
prune_unused_runtimes

echo "Hermes deployed from tag $source_tag at $release"
echo "Remote archive cleaned: $archive"
if [ "$prune_releases" = "1" ]; then
  echo "Release retention: kept newest $keep_releases releases plus protected current/deployed releases"
else
  echo "Release retention: pruning disabled"
fi
echo "Runtime retention: $runtime_pruning_status"
`;
}

async function deployArchive(args, archivePath) {
  const remoteRoot = args.remoteRoot.replace(/\/+$/, "");
  const stagingId = args.dryRun ? "dry-run" : randomUUID();
  const remoteArchive = `${remoteRoot}/tmp/hermes-${args.releaseId}-${stagingId}.tar.gz`;

  await runSsh(args, ["mkdir", "-p", `${remoteRoot}/tmp`, `${remoteRoot}/releases`, `${remoteRoot}/shared/.hermes`]);
  await runScp(args, archivePath, remoteArchive);
  return runSsh(
    args,
    [
      "bash",
      "-s",
      "--",
      remoteRoot,
      args.releaseId,
      args.sourceCommit,
      args.sourceTag,
      remoteArchive,
      String(args.keepReleases),
      args.pruneReleases ? "1" : "0",
      args.dashboardPublicUrl,
      args.migrateNginxHermes ? "1" : "0",
      args.dashboardPublicHost,
      args.provisionPowerpointDeps ? "1" : "0",
      DEFAULT_PYTHON_PACKAGE_INDEX,
      args.pruneRuntimes ? "1" : "0",
    ],
    { input: remoteDeployScript() },
  );
}

function parseSmokeResult(output, kind) {
  const lines = String(output || "").split(/\r?\n/).reverse();
  for (const line of lines) {
    if (!line.includes(`\"kind\":\"${kind}\"`) && !line.includes(`\"kind\": \"${kind}\"`)) {
      continue;
    }
    try {
      const result = JSON.parse(line);
      if (result?.kind === kind) {
        return result;
      }
    } catch {
      // The remote stage validates its JSON; continue past unrelated output.
    }
  }
  return null;
}

function runContinuityConversationSmoke(args, phase) {
  const python = args.pythonCommand || resolvePythonCommand();
  const commandArgs = [
    ...python.argsPrefix,
    path.join(repoRoot, "scripts", "smoke_dashboard_conversation.py"),
    "--url",
    args.dashboardPublicUrl,
    "--timeout",
    "240",
    "--session",
    "hermes-release-continuity",
    "--continuity-phase",
    phase,
  ];
  try {
    const commandResult = run(python.command, commandArgs, { dryRun: args.dryRun });
    return {
      status: args.dryRun ? "planned" : "passed",
      result: args.dryRun
        ? null
        : parseSmokeResult(commandResult.stdout, "hermes.public-continuity-smoke"),
    };
  } catch (error) {
    console.error(`Public dashboard continuity ${phase} failed: ${error.message}`);
    return {
      status: "failed",
      result: parseSmokeResult(error?.commandResult?.stdout, "hermes.public-continuity-smoke"),
    };
  }
}

function runPublicConversationSmoke(args) {
  const python = args.pythonCommand || resolvePythonCommand();
  const commandArgs = [
    ...python.argsPrefix,
    path.join(repoRoot, "scripts", "smoke_dashboard_conversation.py"),
    "--url",
    args.dashboardPublicUrl,
    "--timeout",
    "180",
  ];
  try {
    const commandResult = run(python.command, commandArgs, { dryRun: args.dryRun });
    return {
      status: args.dryRun ? "planned" : "passed",
      result: args.dryRun
        ? null
        : parseSmokeResult(commandResult.stdout, "hermes.public-conversation-smoke"),
    };
  } catch (error) {
    console.error(`Public dashboard conversation smoke failed: ${error.message}`);
    return {
      status: "failed",
      result: parseSmokeResult(error?.commandResult?.stdout, "hermes.public-conversation-smoke"),
    };
  }
}

function remoteStagePassed(error, stage) {
  const output = [error?.commandResult?.stdout, error?.commandResult?.stderr]
    .filter(Boolean)
    .join("\n");
  return output.includes(`HERMES_DEPLOY_STAGE ${stage}=passed`);
}

function authorityConcurrencySummary(smoke) {
  if (!smoke) {
    return null;
  }
  const observations = smoke.observations || {};
  const checkpoint = observations.checkpoint || {};
  return {
    health: [
      `checkpointBusy=${checkpoint.busy ?? "?"}`,
      `integrity=${observations.integrity ?? "?"}`,
      `schema=${observations.schemaVersion ?? "?"}`,
      `recoveryRequired=${observations.recoveryRequired ?? "?"}`,
    ].join(" "),
    cleanup: Object.values(smoke.cleanup || {}).every(Boolean) ? "passed" : "failed",
    failure: smoke.failure || null,
  };
}

function readerPerformanceSummary(smoke) {
  if (!smoke) {
    return null;
  }
  const observations = smoke.observations || {};
  const sql = observations.sql || {};
  const checks = new Map((smoke.checks || []).map((check) => [check.name, check]));
  return {
    sql: `list=${sql.list ?? "?"} stats=${sql.stats ?? "?"} search=${sql.search ?? "?"}`,
    latency: [
      `local=${observations.localListMs ?? "?"}ms`,
      `cold=${checks.get("reader_uds_cold")?.observedMs ?? "?"}ms`,
      `warm=${checks.get("reader_uds_warm")?.observedMs ?? "?"}ms`,
    ].join(" "),
    resources: Object.entries(observations.resources || {})
      .map(([key, value]) => `${key}=${value}`)
      .join(" "),
    cleanup: Object.values(smoke.cleanup || {}).every(Boolean) ? "passed" : "failed",
    failure: smoke.failure || null,
  };
}

function publicReaderLatency(smoke) {
  const checks = new Map((smoke?.checks || []).map((check) => [check.name, check]));
  const list = checks.get("public_session_reader_list")?.durationMs;
  const messages = checks.get("public_session_reader_messages")?.durationMs;
  return list === undefined || messages === undefined
    ? null
    : `list=${list}ms messages=${messages}ms`;
}

function printSummary(args, result) {
  const remoteRoot = args.remoteRoot.replace(/\/+$/, "");
  const target = `${args.user}@${args.host}`;
  console.log(`\nRelease validation summary`);
  console.log(`Deploy target: ${target}:${remoteRoot}`);
  console.log(`Tag: ${args.sourceTag}`);
  console.log(`Current symlink: ${remoteRoot}/current -> ${remoteRoot}/releases/${args.releaseId}`);
  console.log(`State dir: ${remoteRoot}/shared/.hermes`);
  console.log(`Env file: ${remoteRoot}/shared/.env`);
  console.log(`Service: hermes-dashboard.service`);
  console.log(`Dashboard: ${args.dashboardPublicUrl} (Hermes user login only)`);
  console.log(
    `Nginx: ${args.migrateNginxHermes ? "explicit legacy-block migration" : "managed snippet reconciliation"}`,
  );
  console.log(`PowerPoint runtime smoke: ${result.powerpointSmoke}`);
  console.log(`PowerPoint host provisioning: ${args.provisionPowerpointDeps ? "enabled" : "preflight only"}`);
  console.log(`Authority concurrency smoke: ${result.authorityConcurrencySmoke}`);
  const authoritySummary = authorityConcurrencySummary(result.authorityConcurrencyResult);
  if (authoritySummary) {
    console.log(`Authority checkpoint/integrity/schema/recovery: ${authoritySummary.health}`);
    console.log(`Authority cleanup: ${authoritySummary.cleanup}`);
    if (authoritySummary.failure) {
      console.log(`Authority failure: ${authoritySummary.failure.code}/${authoritySummary.failure.check}`);
    }
  }
  console.log(`Session Reader performance smoke: ${result.readerPerformanceSmoke}`);
  const readerSummary = readerPerformanceSummary(result.readerPerformanceResult);
  if (readerSummary) {
    console.log(`Session Reader SQL: ${readerSummary.sql}`);
    console.log(`Session Reader latency: ${readerSummary.latency}`);
    console.log(`Session Reader resources: ${readerSummary.resources}`);
    console.log(`Session Reader cleanup: ${readerSummary.cleanup}`);
    if (readerSummary.failure) {
      console.log(`Session Reader failure: ${readerSummary.failure.code}/${readerSummary.failure.check}`);
    }
  }
  console.log(`Deterministic conversation smoke: ${result.deterministicSmoke}`);
  if (result.deterministicSmokeResult?.failure) {
    const failure = result.deterministicSmokeResult.failure;
    console.log(`Deterministic failure: ${failure.code}/${failure.check}`);
  }
  console.log(`Cross-release conversation continuity: ${result.continuitySmoke}`);
  console.log(`Public real-AI conversation smoke: ${result.publicSmoke}`);
  const publicLatency = publicReaderLatency(result.publicSmokeResult);
  if (publicLatency) {
    console.log(`Public Session Reader latency (informational): ${publicLatency}`);
  }
  console.log(`Release outcome: ${result.outcome}`);
  console.log("Remote staging archive and deterministic smoke state are removed after use.");
  console.log(
    args.pruneReleases
      ? `Release retention: keep newest ${args.keepReleases} releases plus protected current/deployed releases`
      : `Release retention: disabled (--no-prune-releases)`,
  );
  console.log(
    args.pruneRuntimes
      ? "Runtime retention: remove managed runtimes not referenced by running processes"
      : "Runtime retention: disabled (--no-prune-runtimes)",
  );
  console.log(`Status: ssh ${target} 'systemctl status --no-pager hermes-dashboard'`);
  console.log("Rollback example: npm run deploy -- --tag <previous-tag>");
}

export async function main({ argv = process.argv.slice(2), cwd = repoRoot } = {}) {
  const args = parseArgs(argv);
  if (args.help) {
    usage();
    return;
  }

  if (args.checkConnection) {
    if (!process.env.HERMES_DEPLOY_PASSWORD) {
      requireBinary("ssh");
    }
    const result = await runSsh(args, [
      "bash",
      "-lc",
      "printf 'HERMES_DEPLOY_CONNECTION_OK\\n'; uname -s; test -x /bin/bash",
    ]);
    if (!args.dryRun && !result.stdout.includes("HERMES_DEPLOY_CONNECTION_OK")) {
      throw new Error("SSH connection check completed without the expected marker.");
    }
    console.log(`Connection check passed: ${args.user}@${args.host}:${args.port}`);
    return;
  }

  requireBinary("git");
  requireBinary("npm");
  if (!args.dryRun && !process.env.HERMES_DEPLOY_PASSWORD) {
    requireBinary("ssh");
    requireBinary("scp");
  }
  args.pythonCommand = resolvePythonCommand();

  if (args.force) {
    throw new Error("--force is no longer supported for immutable releases.");
  }

  if (args.createTag) {
    const prepared = prepareCreateTag(args.createTag, {
      allowNonMain: args.allowNonMain,
      dryRun: args.dryRun,
      cwd,
    });
    args.sourceTag = args.createTag;
    args.sourceCommit = prepared.sourceCommit;
  } else {
    validateTag(args.tag);
    assertCleanWorktree({ allowDirty: args.allowDirty, dryRun: args.dryRun, cwd });
    if (!tagExists(args.tag, { cwd })) {
      throw new Error(`Tag does not exist locally: ${args.tag}. Run 'git fetch --tags' first if needed.`);
    }
    const localTagCommit = runText("git", ["rev-parse", "--verify", `${args.tag}^{commit}`], { cwd });
    const originTagCommit = remoteTagCommit(args.tag, { cwd });
    if (!originTagCommit) {
      throw new Error(`Tag does not exist on origin: ${args.tag}. --tag is only for retrying or rolling back published tags.`);
    }
    if (originTagCommit !== localTagCommit) {
      throw new Error(`Local and origin tag '${args.tag}' do not resolve to the same commit.`);
    }
    args.sourceTag = args.tag;
    args.sourceCommit = localTagCommit;
  }

  args.releaseId = args.sourceTag;
  const { tmp, archivePath } = createArchive(args, { dryRun: args.dryRun, cwd });
  let deploymentCommitted = false;
  let authorityConcurrencyResult = null;
  let readerPerformanceResult = null;
  let continuityPrepare = null;
  try {
    continuityPrepare = args.initialContinuityTransition
      ? { status: args.dryRun ? "planned (initial transition)" : "not supported by old release", result: null }
      : runContinuityConversationSmoke(args, "prepare");
    if (!args.dryRun && continuityPrepare.status === "failed") {
      throw new Error("cross-release continuity preparation failed before remote deployment");
    }
    try {
      const remoteResult = await deployArchive(args, archivePath);
      authorityConcurrencyResult = args.dryRun
        ? null
        : parseSmokeResult(remoteResult.stdout, "hermes.authority-concurrency-smoke");
      readerPerformanceResult = args.dryRun
        ? null
        : parseSmokeResult(remoteResult.stdout, "hermes.session-reader-performance-smoke");
      deploymentCommitted = args.dryRun || remoteResult.stdout.includes("HERMES_DEPLOY_STAGE deployment=committed");
      if (!deploymentCommitted) {
        throw new Error("remote deployment completed without a commit marker");
      }
    } catch (error) {
      authorityConcurrencyResult = parseSmokeResult(
        error?.commandResult?.stdout,
        "hermes.authority-concurrency-smoke",
      );
      readerPerformanceResult = parseSmokeResult(
        error?.commandResult?.stdout,
        "hermes.session-reader-performance-smoke",
      );
      printSummary(args, {
        powerpointSmoke: args.dryRun
          ? "planned"
          : remoteStagePassed(error, "powerpoint_runtime_smoke")
            ? "passed"
            : "failed or not reached",
        authorityConcurrencySmoke: args.dryRun
          ? "planned"
          : remoteStagePassed(error, "authority_concurrency_smoke")
            ? "passed"
            : "failed or not reached",
        authorityConcurrencyResult,
        readerPerformanceSmoke: args.dryRun
          ? "planned"
          : remoteStagePassed(error, "session_reader_performance_smoke")
            ? "passed"
            : "failed or not reached",
        readerPerformanceResult,
        deterministicSmoke: "failed or not reached",
        deterministicSmokeResult: parseSmokeResult(
          error?.commandResult?.stdout,
          "hermes.conversation-smoke",
        ),
        continuitySmoke:
          args.initialContinuityTransition || args.dryRun || continuityPrepare?.status !== "passed"
            ? continuityPrepare?.status || "not run"
            : runContinuityConversationSmoke(args, "verify").status,
        publicSmoke: "not run",
        publicSmokeResult: null,
        outcome: "rolled back before commit",
      });
      throw error;
    }

    const continuitySmoke = args.initialContinuityTransition
      ? { status: args.dryRun ? "planned (initial transition)" : "not supported by old release", result: null }
      : runContinuityConversationSmoke(args, "verify");
    const publicSmoke = runPublicConversationSmoke(args);
    const allPublicSmokePassed =
      args.dryRun ||
      ((args.initialContinuityTransition || continuitySmoke.status === "passed") &&
        publicSmoke.status === "passed");
    const outcome = args.dryRun
      ? "dry-run: deployment and all smoke layers planned"
      : allPublicSmokePassed
        ? "deployment committed and all smoke passed"
        : "deployment committed but public smoke failed";
    printSummary(args, {
      powerpointSmoke: args.dryRun ? "planned" : "passed",
      authorityConcurrencySmoke: args.dryRun ? "planned" : "passed",
      authorityConcurrencyResult,
      readerPerformanceSmoke: args.dryRun ? "planned" : "passed",
      readerPerformanceResult,
      deterministicSmoke: args.dryRun ? "planned" : "passed",
      continuitySmoke: continuitySmoke.status,
      publicSmoke: publicSmoke.status,
      publicSmokeResult: publicSmoke.result,
      outcome,
    });
    if (!allPublicSmokePassed) {
      throw new Error("deployment committed but public smoke failed; automatic rollback was not attempted");
    }
  } finally {
    if (tmp && !args.dryRun) {
      rmSync(tmp, { recursive: true, force: true });
    }
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    console.error(`deploy failed: ${error.message}`);
    process.exitCode = 1;
  });
}
