import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const repoRoot = path.resolve(import.meta.dirname, "../../../..");
const pluginSource = path.join(
  repoRoot,
  "plugins/scheduled-tasks/dashboard/src/index.tsx",
);
const pluginBundle = path.join(
  repoRoot,
  "plugins/scheduled-tasks/dashboard/dist/index.js",
);

describe("scheduled tasks dashboard plugin", () => {
  it("self-registers with the host and uses the authenticated plugin API", () => {
    const source = readFileSync(pluginSource, "utf8");

    expect(source).toContain('register("scheduled-tasks", ScheduledTasksPage)');
    expect(source).toContain('const API_ROOT = "/api/plugins/scheduled-tasks"');
    expect(source).toContain('body: JSON.stringify({ updates })');
    expect(source).toContain('PluginSlot name="cron:top"');
    expect(source).toContain('PluginSlot name="cron:bottom"');
    expect(source).not.toContain("__HERMES_SESSION_TOKEN__");
    expect(source).not.toMatch(/^import(?!\s+type).*from ["']react["']/m);
  });

  it("builds against host React without embedding credentials or a React runtime", () => {
    const bundle = readFileSync(pluginBundle, "utf8");

    expect(bundle).toContain("__HERMES_PLUGIN_SDK__");
    expect(bundle).toContain("scheduled-tasks");
    expect(bundle).not.toContain("__HERMES_SESSION_TOKEN__");
    expect(bundle).not.toMatch(/react\.production|react-jsx-runtime|ReactCurrentDispatcher/);
  });
});
