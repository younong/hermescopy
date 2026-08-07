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

    const container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root?.render(<ChannelsPage />);
      await Promise.resolve();
      await Promise.resolve();
    });

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
});
