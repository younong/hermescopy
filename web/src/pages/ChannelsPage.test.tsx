// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "@/lib/api";
import ChannelsPage from "./ChannelsPage";

vi.mock("@/lib/useDashboardAuthIdentity", () => ({
  useDashboardAuthIdentity: () => ({ authRequired: true }),
}));

let root: Root | null = null;

async function renderChannelsPage() {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(<ChannelsPage />);
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = "";
});

afterEach(async () => {
  await act(async () => root?.unmount());
  root = null;
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("ChannelsPage", () => {
  it("shows managed Feishu employees without loading legacy platforms", async () => {
    const getMessagingPlatforms = vi.spyOn(api, "getMessagingPlatforms");
    vi.spyOn(api, "getFeishuEmployees").mockResolvedValue({ employees: [] });
    vi.spyOn(api, "getFeishuEmployeeCatalog").mockResolvedValue({
      knowledge_roots: [],
      mcp_servers: [],
      model_registrations: [],
      skills: [],
      toolsets: [],
      workspace: { default: "default", root: "" },
    });

    await renderChannelsPage();

    expect(getMessagingPlatforms).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain("Feishu / Lark");
    expect(document.body.textContent).toContain("AI employees");
    expect(document.body.textContent).toContain("No managed Feishu employees yet.");
    expect(document.body.textContent).not.toContain("0 of 0 channels configured");

    const addEmployee = Array.from(document.querySelectorAll("button")).find(
      (button) => button.textContent === "Add employee",
    );
    expect(addEmployee?.className).toContain("bg-midground");
    expect(addEmployee?.className).toContain("text-background-base");
  });

  it("uses catalog options and puts credentials last in the employee form", async () => {
    vi.spyOn(api, "getFeishuEmployees").mockResolvedValue({ employees: [] });
    vi.spyOn(api, "getFeishuEmployeeCatalog").mockResolvedValue({
      knowledge_roots: [],
      mcp_servers: ["unused-server"],
      model_registrations: [{ id: "model-1", name: "Model One" }],
      skills: [{ name: "existing-skill", description: "Existing skill" }],
      toolsets: [{ name: "terminal", description: "Terminal tools" }],
      workspace: { default: "default", root: "" },
    });

    await renderChannelsPage();

    const addButton = [...document.querySelectorAll("button")].find(
      (button) => button.textContent === "Add employee",
    );
    await act(async () => addButton?.click());

    const dialog = document.querySelector('[role="dialog"]');
    const formText = dialog?.textContent ?? "";
    expect(formText).toContain("existing-skill");
    expect(formText).toContain("terminal");
    expect(dialog?.querySelectorAll('input[type="checkbox"]')).toHaveLength(2);
    expect(formText).not.toContain("Workspace relative path");
    expect(formText).not.toContain("Knowledge relative paths");
    expect(formText).not.toContain("MCP servers");

    expect(formText.indexOf("Feishu / Lark app credentials")).toBeGreaterThan(
      formText.indexOf("Max iterations"),
    );
  });
});
