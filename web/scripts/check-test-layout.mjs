// Enforces the web test layout convention: every *.test.{ts,tsx} file under
// src/ must live inside a __tests__/ directory next to the code it tests.
// vitest only includes src/**/__tests__/**/*.test.{ts,tsx}, so a test placed
// anywhere else would be silently skipped; this check fails the test run
// instead. Wired into `npm test` via the pretest script.
import { readdirSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const srcRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../src");
const TEST_FILE_RE = /\.test\.(ts|tsx)$/;

function collectMisplaced(dir, offenders) {
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) {
      collectMisplaced(full, offenders);
    } else if (TEST_FILE_RE.test(entry) && path.basename(dir) !== "__tests__") {
      offenders.push(path.relative(srcRoot, full));
    }
  }
}

const offenders = [];
collectMisplaced(srcRoot, offenders);

if (offenders.length > 0) {
  console.error("Test files must live in a __tests__/ directory next to the code they test:");
  for (const file of offenders) {
    console.error(`  src/${file}`);
  }
  process.exit(1);
}
