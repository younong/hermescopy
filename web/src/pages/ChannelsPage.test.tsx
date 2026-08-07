// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api, type FeishuEmployee } from "@/lib/api";
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

function changeValue(element: HTMLInputElement | HTMLTextAreaElement | null, value: string) {
  if (!element) throw new Error("Expected form control");
  const prototype = element instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype
    : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
  act(() => {
    setter?.call(element, value);
    element.dispatchEvent(new Event("input", { bubbles: true }));
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
    const createdEmployee = {
      account_id: "ca_test",
      app_id: "cli_test",
      avatar_url: null,
      credential_version: 1,
      lifecycle_status: "active" as const,
      runtime_state: "ready",
      profile_revision: 1,
      profile_fingerprint: "sha256:test",
      profile: null,
    };
    const createFeishuEmployee = vi.spyOn(api, "createFeishuEmployee").mockResolvedValue(createdEmployee);
    const uploadAvatar = vi.spyOn(api, "uploadFeishuEmployeeAvatar").mockResolvedValue({ avatar_url: "/avatar" });
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
    expect(formText).not.toContain("Toolsets");
    expect(dialog?.querySelectorAll('input[type="checkbox"]')).toHaveLength(1);
    expect(formText).not.toContain("Workspace relative path");
    expect(formText).not.toContain("Knowledge relative paths");
    expect(formText).not.toContain("MCP servers");

    expect(formText.indexOf("Feishu / Lark app credentials")).toBeGreaterThan(
      formText.indexOf("Max iterations"),
    );

    const credentialInputs = dialog?.querySelector("fieldset")?.querySelectorAll("input") ?? [];
    const appId = [...credentialInputs].find((input) => input.type === "text") ?? null;
    const appSecret = [...credentialInputs].find((input) => input.type === "password") ?? null;
    changeValue(appId, "cli_test");
    changeValue(appSecret, "secret");
    changeValue(dialog?.querySelector("textarea") ?? null, "Help users.");
    const avatar = new File(["avatar"], "avatar.png", { type: "image/png" });
    const avatarInput = dialog?.querySelector<HTMLInputElement>('input[type="file"]');
    Object.defineProperty(avatarInput, "files", { value: [avatar], configurable: true });
    await act(async () => avatarInput?.dispatchEvent(new Event("change", { bubbles: true })));
    const saveButton = [...(dialog?.querySelectorAll("button") ?? [])].find(
      (button) => button.textContent === "Save",
    );
    await act(async () => saveButton?.click());

    expect(createFeishuEmployee).toHaveBeenCalledWith(
      expect.objectContaining({
        profile: expect.objectContaining({ toolsets: ["terminal"] }),
      }),
    );
    expect(uploadAvatar).toHaveBeenCalledWith("ca_test", avatar);
  });

  it("renders employee avatars and removes them from profile editing", async () => {
    const employee: FeishuEmployee = {
      account_id: "ca_avatar",
      app_id: "cli_avatar",
      avatar_url: "/api/messaging/feishu/employees/ca_avatar/avatar",
      credential_version: 1,
      lifecycle_status: "active" as const,
      runtime_state: "ready",
      profile_revision: 2,
      profile_fingerprint: "sha256:test",
      profile: {
        schema_version: 1,
        name: "Ada",
        role: "Engineer",
        model_registration_id: "model-1",
        system_prompt: "Build carefully.",
        toolsets: [],
        skills: [],
        mcp_servers: [],
        workspace_relative_path: "employees/ada",
        knowledge_relative_paths: [],
        max_iterations: 20,
      },
    };
    vi.spyOn(api, "getFeishuEmployees").mockResolvedValue({ employees: [employee] });
    vi.spyOn(api, "getFeishuEmployeeCatalog").mockResolvedValue({
      knowledge_roots: [],
      mcp_servers: [],
      model_registrations: [{ id: "model-1", name: "Model One" }],
      skills: [],
      toolsets: [{ name: "terminal", description: "Terminal tools" }],
      workspace: { default: "default", root: "" },
    });
    vi.spyOn(api, "updateFeishuEmployeeProfile").mockResolvedValue({ ...employee, profile_revision: 3 });
    const deleteAvatar = vi.spyOn(api, "deleteFeishuEmployeeAvatar").mockResolvedValue({ ok: true, deleted: true });

    await renderChannelsPage();

    const rowAvatar = document.querySelector<HTMLImageElement>('img[src$="/avatar"]');
    expect(rowAvatar).not.toBeNull();
    await act(async () => rowAvatar?.dispatchEvent(new Event("error")));
    expect(document.body.textContent).toContain("A");
    const editButton = [...document.querySelectorAll("button")].find(
      (button) => button.textContent === "Edit policy",
    );
    await act(async () => editButton?.click());
    const dialog = document.querySelector('[role="dialog"]');
    const removeButton = [...(dialog?.querySelectorAll("button") ?? [])].find(
      (button) => button.textContent === "Remove",
    );
    await act(async () => removeButton?.click());
    const saveButton = [...(dialog?.querySelectorAll("button") ?? [])].find(
      (button) => button.textContent === "Save",
    );
    await act(async () => saveButton?.click());

    expect(deleteAvatar).toHaveBeenCalledWith("ca_avatar");
  });
});
