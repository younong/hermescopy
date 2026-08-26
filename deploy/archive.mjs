import { rmSync, statSync } from "node:fs";
import path from "node:path";
import * as tar from "tar";

const OMITTED_ROOTS = new Set(["node_modules", "tests", "website", ".github", "docs"]);
const OMITTED_DIRECTORIES = new Set(["web/node_modules"]);
const OMITTED_FILES = new Set([
  "deploy/powerpoint-runtime/runtime-modules/.package-lock.json",
]);

function normalizeArchivePath(entryPath) {
  return entryPath.replaceAll("\\", "/").replace(/^\.\//, "").replace(/\/$/, "");
}

function isSafeRelativePath(entryPath) {
  const normalized = normalizeArchivePath(entryPath);
  if (!normalized) return true;
  return !path.posix.isAbsolute(normalized) && !normalized.split("/").includes("..");
}

export function shouldIncludeReleasePath(entryPath) {
  const normalized = normalizeArchivePath(entryPath);
  if (!isSafeRelativePath(normalized)) return false;
  if (!normalized) return true;
  if (normalized.split("/").some((part) => part.startsWith("._"))) return false;
  const root = normalized.split("/", 1)[0];
  if (OMITTED_ROOTS.has(root)) return false;
  for (const directory of OMITTED_DIRECTORIES) {
    if (normalized === directory || normalized.startsWith(`${directory}/`)) return false;
  }
  return !OMITTED_FILES.has(normalized);
}

export function extractSourceArchive(sourceArchive, buildDir) {
  tar.extract({
    cwd: buildDir,
    file: sourceArchive,
    preservePaths: false,
    strict: true,
    sync: true,
    filter(entryPath) {
      if (!isSafeRelativePath(entryPath)) {
        throw new Error(`Unsafe path in source archive: ${entryPath}`);
      }
      return true;
    },
  });
  rmSync(sourceArchive, { force: true });
}

export function createReleaseArchiveFile(buildDir, archivePath, { gitModes = new Map() } = {}) {
  tar.create(
    {
      cwd: buildDir,
      file: archivePath,
      follow: false,
      gzip: true,
      portable: true,
      strict: true,
      sync: true,
      filter: shouldIncludeReleasePath,
      onWriteEntry(entry) {
        const normalized = normalizeArchivePath(entry.path);
        const gitMode = gitModes.get(normalized);
        if (entry.type === "Directory") {
          entry.stat.mode = 0o755;
        } else if (gitMode === "100755") {
          entry.stat.mode = 0o755;
        } else if (entry.type === "File") {
          entry.stat.mode = 0o644;
        }
      },
    },
    ["."],
  );
  return statSync(archivePath).size;
}
