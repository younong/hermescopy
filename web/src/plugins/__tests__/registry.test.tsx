// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  exposePluginSDK,
  getPluginWorkspaceComponent,
  onPluginRegistered,
} from "../registry";

const Workspace = () => null;

afterEach(() => {
  delete window.__HERMES_PLUGINS__;
  delete window.__HERMES_PLUGIN_SDK__;
});

describe("plugin workspace registry", () => {
  it("registers workspace components and notifies subscribers", () => {
    const listener = vi.fn();
    const unsubscribe = onPluginRegistered(listener);
    exposePluginSDK();

    window.__HERMES_PLUGINS__?.registerWorkspace("tools", "notes", Workspace);

    expect(getPluginWorkspaceComponent("tools", "notes")).toBe(Workspace);
    expect(listener).toHaveBeenCalledOnce();
    unsubscribe();
  });
});
