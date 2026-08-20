import { access, readdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "vite";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const pluginsRoot = path.resolve(webRoot, "../plugins");
const mode = process.argv[2] ?? "build";

const candidates = (await readdir(pluginsRoot, { withFileTypes: true }))
  .filter((entry) => entry.isDirectory())
  .map((entry) => ({
    name: entry.name,
    source: path.join(pluginsRoot, entry.name, "dashboard", "src", "index.tsx"),
    dist: path.join(pluginsRoot, entry.name, "dashboard", "dist"),
  }))
  .sort((left, right) => left.name.localeCompare(right.name));
const plugins = [];
for (const candidate of candidates) {
  try {
    await access(candidate.source);
    plugins.push(candidate);
  } catch {
    // Dashboard plugins without first-party TypeScript source are prebuilt.
  }
}

for (const plugin of plugins) {
  await build({
    configFile: false,
    publicDir: false,
    esbuild: {
      jsx: "transform",
      jsxFactory: "window.__HERMES_PLUGIN_SDK__.React.createElement",
      jsxFragment: "window.__HERMES_PLUGIN_SDK__.React.Fragment",
    },
    logLevel: "silent",
    build: {
      emptyOutDir: mode === "build",
      lib: {
        entry: plugin.source,
        fileName: () => "index.js",
        formats: ["iife"],
        name: `HermesDashboardPlugin_${plugin.name.replaceAll("-", "_")}`,
      },
      minify: mode === "build",
      outDir: plugin.dist,
      target: "es2022",
      write: mode === "build",
    },
  });
  if (mode === "build") {
    try {
      await access(path.join(plugin.dist, "style.css"));
    } catch {
      await writeFile(path.join(plugin.dist, "style.css"), "");
    }
  }
}

console.log(`${mode === "build" ? "built" : "checked"} ${plugins.length} dashboard plugin(s)`);
