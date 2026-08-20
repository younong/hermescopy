// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { PluginProvider, usePlugins } from "../PluginProvider";
import type { PluginManifest } from "../types";

vi.mock("@/lib/api", () => ({
  HERMES_BASE_PATH: "/hermes",
  api: { getPlugins: vi.fn() },
}));

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  vi.mocked(api.getPlugins).mockReset();
  document.head.innerHTML = "";
  document.body.innerHTML = "";
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  document.head.innerHTML = "";
  document.body.innerHTML = "";
});

describe("PluginProvider", () => {
  it("loads all admin plugin assets once and preserves script integrity", async () => {
    vi.mocked(api.getPlugins).mockResolvedValue([
      manifest("admin-page", { tab: { path: "/admin-page" }, css: "style.css", integrity: "sha384-test" }),
    ]);

    renderProvider("admin");
    await flush();

    expect(document.querySelectorAll('script[data-hermes-plugin="admin-page"]')).toHaveLength(1);
    expect(document.querySelectorAll('link[data-hermes-plugin="admin-page"]')).toHaveLength(1);
    const script = document.querySelector('script[data-hermes-plugin="admin-page"]') as HTMLScriptElement;
    expect(script.src).toContain("/hermes/dashboard-plugins/admin-page/index.js");
    expect(script.integrity).toBe("sha384-test");
    expect(script.crossOrigin).toBe("anonymous");

    await act(async () => root?.unmount());
    root = null;
    renderProvider("admin");
    await flush();

    expect(document.querySelectorAll('script[data-hermes-plugin="admin-page"]')).toHaveLength(1);
    expect(document.querySelectorAll('link[data-hermes-plugin="admin-page"]')).toHaveLength(1);
  });

  it("keeps loading until a workspace registers or its script reaches a terminal failure", async () => {
    vi.mocked(api.getPlugins).mockResolvedValue([
      manifest("slow-workspace", {
        chat: { workspaces: [{
          id: "notes",
          path: "/chat/notes",
          label: "Notes",
          description: "",
          icon: "Puzzle",
          position: "end",
          admin_only: false,
        }] },
      }),
    ]);

    renderProvider("member");
    await flush();
    expect(document.querySelector("[data-loading]")?.textContent).toBe("loading");

    const script = document.querySelector(
      'script[data-hermes-plugin="slow-workspace"]',
    ) as HTMLScriptElement;
    await act(async () => {
      script.onload?.(new Event("load"));
    });
    expect(document.querySelector("[data-loading]")?.textContent).toBe("ready");
  });

  it("only exposes and loads member-safe workspace manifests for members", async () => {
    vi.mocked(api.getPlugins).mockResolvedValue([
      manifest("admin-page", { tab: { path: "/admin-page" } }),
      manifest("admin-chat-tools", {
        chat: { workspaces: [{
          id: "operations",
          path: "/chat/operations",
          label: "Operations",
          description: "",
          icon: "Puzzle",
          position: "end",
          admin_only: true,
        }] },
      }),
      manifest("chat-tools", {
        chat: { workspaces: [{
          id: "notes",
          path: "/chat/notes",
          label: "Notes",
          description: "",
          icon: "Puzzle",
          position: "end",
          admin_only: false,
        }] },
      }),
    ]);

    renderProvider("member");
    await flush();

    expect(document.querySelector('script[data-hermes-plugin="admin-page"]')).toBeNull();
    expect(document.querySelector('script[data-hermes-plugin="admin-chat-tools"]')).toBeNull();
    expect(document.querySelector('script[data-hermes-plugin="chat-tools"]')).not.toBeNull();
    expect(document.querySelector("[data-manifests]")?.textContent).toBe("chat-tools");
  });
});

function Probe() {
  const { loading, manifests } = usePlugins();
  return (
    <>
      <div data-loading>{loading ? "loading" : "ready"}</div>
      <div data-manifests>{manifests.map((manifest) => manifest.name).join(",")}</div>
    </>
  );
}

function renderProvider(mode: "admin" | "member") {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <PluginProvider mode={mode}>
        <Probe />
      </PluginProvider>,
    );
  });
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function manifest(
  name: string,
  overrides: Partial<PluginManifest>,
): PluginManifest {
  return {
    name,
    label: name,
    description: "",
    icon: "Puzzle",
    version: "1.0.0",
    entry: "index.js",
    css: null,
    has_api: false,
    source: "test",
    ...overrides,
  };
}
