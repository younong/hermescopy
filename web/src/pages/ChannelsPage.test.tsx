// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { I18nProvider } from "@/i18n";
import { api, type Employee } from "@/lib/api";
import ChannelsPage from "./ChannelsPage";

vi.mock("@/lib/useDashboardAuthIdentity", () => ({
  useDashboardAuthIdentity: () => ({ authRequired: true }),
}));

let root: Root | null = null;

async function renderChannelsPage(locale: "en" | "zh" = "en") {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  localStorage.setItem("hermes-locale", locale);
  await act(async () => {
    root?.render(<I18nProvider><MemoryRouter><ChannelsPage /></MemoryRouter></I18nProvider>);
    await Promise.resolve();
    await Promise.resolve();
  });
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = "";
  localStorage.clear();
});

afterEach(async () => {
  await act(async () => root?.unmount());
  root = null;
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("ChannelsPage", () => {
  it("shows managed channel status and links employee management to the Chat GUI", async () => {
    const getMessagingPlatforms = vi.spyOn(api, "getMessagingPlatforms");
    vi.spyOn(api, "getEmployees").mockResolvedValue({ employees: [] });

    await renderChannelsPage();

    expect(getMessagingPlatforms).not.toHaveBeenCalled();
    expect(document.body.textContent).toContain("Feishu / Lark");
    expect(document.body.textContent).toContain("No employee Feishu / Lark bindings are configured.");
    const link = document.querySelector<HTMLAnchorElement>('a[href="/chat/contacts"]');
    expect(link?.textContent).toBe("Manage employees");
    expect(document.body.textContent).not.toContain("Add employee");
    expect(document.body.textContent).not.toContain("May participate");
    expect(document.body.textContent).not.toContain("Edit profile");
  });

  it("summarizes binding status without duplicating profile or lifecycle controls", async () => {
    const employees: Employee[] = [
      employee("employee-a", "active", "running"),
      employee("employee-b", "suspended", "stopped"),
    ];
    vi.spyOn(api, "getEmployees").mockResolvedValue({ employees });

    await renderChannelsPage();

    expect(document.body.textContent).toContain("2 employee bindings: 1 active.");
    expect(document.body.textContent).not.toContain("Researcher");
    expect(document.querySelector('button[aria-label="Enable Feishu / Lark"]')).toBeNull();
  });
  it("renders the managed employee support surface in Chinese", async () => {
    vi.spyOn(api, "getEmployees").mockResolvedValue({ employees: [] });

    await renderChannelsPage("zh");

    expect(document.body.textContent).toContain("按员工管理的飞书和 Lark 连接");
    expect(document.body.textContent).toContain("尚未配置员工飞书 / Lark 绑定。");
    expect(document.querySelector<HTMLAnchorElement>('a[href="/chat/contacts"]')?.textContent).toBe("管理员工");
  });

});

function employee(
  employeeId: string,
  lifecycleStatus: "active" | "suspended",
  runtimeState: string,
): Employee {
  return {
    avatar_url: null,
    employee_kind: "managed",
    protected: false,
    chat_eligible: true,
    channels: {
      feishu: {
        connector_account_id: `account-${employeeId}`,
        app_id: `app-${employeeId}`,
        binding_id: `binding-${employeeId}`,
        credential_version: 1,
        lifecycle_status: lifecycleStatus,
        runtime_state: runtimeState,
      },
    },
    collaboration_policy: {
      invite_quota: 5,
      may_create_groups: false,
      may_participate: true,
    },
    employee_id: employeeId,
    lifecycle_status: "active",
    profile: {
      knowledge_relative_paths: [],
      max_iterations: 20,
      max_tokens: null,
      mcp_servers: [],
      model_registration_id: "model-a",
      name: "Researcher",
      role: "Analyst",
      schema_version: 1,
      skills: [],
      system_prompt: "Research carefully.",
      toolsets: [],
      workspace_relative_path: "employees/researcher",
    },
    profile_fingerprint: "sha256:test",
    profile_revision: 1,
  };
}
