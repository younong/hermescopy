// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { I18nProvider } from "@/i18n";
import { ChatPluginWorkspace } from "../ChatPluginWorkspace";
import { exposePluginSDK, setPluginLoadError } from "../registry";

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = "";
  exposePluginSDK();
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
});

describe("ChatPluginWorkspace", () => {
  it("updates from loading to the registered workspace", () => {
    renderWorkspace("workspace-render");
    expect(document.querySelector('[role="status"]')).not.toBeNull();

    act(() => {
      window.__HERMES_PLUGINS__?.registerWorkspace(
        "workspace-render",
        "notes",
        () => <div data-workspace>Plugin notes</div>,
      );
    });

    expect(document.querySelector("[data-workspace]")?.textContent).toBe("Plugin notes");
  });

  it("renders plugin load failures with Chat workspace feedback styles", () => {
    renderWorkspace("workspace-error");
    act(() => setPluginLoadError("workspace-error", "LOAD_FAILED"));

    const error = document.querySelector('[role="alert"]');
    expect(error?.classList.contains("gui-chat-workspace-feedback")).toBe(true);
    expect(error?.classList.contains("is-error")).toBe(true);
  });
});

function renderWorkspace(pluginName: string) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <I18nProvider>
        <ChatPluginWorkspace pluginName={pluginName} workspaceId="notes" />
      </I18nProvider>,
    );
  });
}
