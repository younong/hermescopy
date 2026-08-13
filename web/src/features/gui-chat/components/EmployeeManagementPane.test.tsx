// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider, type Locale } from "@/i18n";
import { api, type Employee, type EmployeeCatalog } from "@/lib/api";
import { EmployeeManagementPane } from "./EmployeeManagementPane";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/api")>(),
  withHermesAssetAuth: (url: string) => `/hermes${url}`,
}));

const catalog: EmployeeCatalog = {
  knowledge_roots: [],
  mcp_servers: [],
  model_registrations: [{ id: "model-a", model: "claude-opus-4-8", name: "Anthropic" }],
  skills: [{ description: "Research", name: "research" }],
  toolsets: [{ description: "Terminal", name: "terminal" }],
  workspace: { default: "default", root: "" },
};

let root: Root | null = null;

async function renderPane(
  locale: Locale = "en",
  onEmployeesChanged?: (employees: Employee[]) => void,
) {
  localStorage.setItem("hermes-locale", locale);
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(<I18nProvider><EmployeeManagementPane onEmployeesChanged={onEmployeesChanged} /></I18nProvider>);
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
  vi.spyOn(api, "getEmployeeCatalog").mockResolvedValue(catalog);
  document.body.innerHTML = "";
});

afterEach(async () => {
  await act(async () => root?.unmount());
  root = null;
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("EmployeeManagementPane", () => {
  it("renders the employee workspace in Chinese", async () => {
    vi.spyOn(api, "getEmployees").mockResolvedValue({ employees: [employee()] });

    await renderPane("zh");

    expect(document.querySelector('[role="list"][aria-label="员工列表"]')).not.toBeNull();
    expect(document.body.textContent).toContain("创建专注的 AI 员工，用于直接对话和内部协作。");
    expect(document.body.textContent).toContain("资料版本 1");
    expect(document.body.textContent).toContain("未连接");
    expect(document.body.textContent).not.toContain("Employee list");
  });

  it("creates an employee without requesting Feishu credentials", async () => {
    vi.spyOn(api, "getEmployees").mockResolvedValue({ employees: [] });
    const createEmployee = vi.spyOn(api, "createEmployee").mockResolvedValue(employee());

    await renderPane();
    await act(async () => document.querySelector<HTMLButtonElement>("button.gui-chat-workspace-primary-button")?.click());

    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog?.textContent).toContain("Add employee");
    expect(dialog?.querySelector<HTMLSelectElement>("select")?.selectedOptions[0]?.textContent)
      .toBe("claude-opus-4-8");
    expect(dialog?.textContent).not.toContain("Anthropic");
    expect(dialog?.textContent).not.toContain("App secret");
    const inputs = Array.from(dialog?.querySelectorAll<HTMLInputElement>("input") ?? [])
      .filter((input) => input.type === "text");
    changeValue(inputs[0] ?? null, "Researcher");
    changeValue(dialog?.querySelector("textarea") ?? null, "Research carefully.");
    const save = Array.from(dialog?.querySelectorAll<HTMLButtonElement>("button") ?? [])
      .find((button) => button.textContent === "Save");
    await act(async () => {
      save?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(createEmployee).toHaveBeenCalledWith({
      activate: true,
      profile: expect.objectContaining({
        model_registration_id: "model-a",
        name: "Researcher",
        system_prompt: "Research carefully.",
        toolsets: ["terminal"],
      }),
    });
  });

  it("edits profile, collaboration, lifecycle, rollover, and optional binding independently", async () => {
    const current = employee();
    vi.spyOn(api, "getEmployees").mockResolvedValue({ employees: [current] });
    const updatePolicy = vi.spyOn(api, "updateEmployeeCollaborationPolicy").mockResolvedValue(current);
    const rollover = vi.spyOn(api, "rolloverEmployeeSessions").mockResolvedValue({ ok: true, retired_sessions: 2 });
    const lifecycle = vi.spyOn(api, "updateEmployeeLifecycle").mockResolvedValue(current);
    const createBinding = vi.spyOn(api, "createEmployeeFeishuBinding").mockResolvedValue({
      ...current,
      channels: { feishu: binding() },
    });

    await renderPane();
    const buttons = () => Array.from(document.querySelectorAll<HTMLButtonElement>("button"));
    const list = document.querySelector('[role="list"][aria-label="Employee list"]');
    expect(list?.querySelectorAll('[role="listitem"]')).toHaveLength(1);
    expect(document.body.textContent).toContain("Employees");
    expect(document.body.textContent).not.toContain("员工");
    expect(list?.textContent).not.toContain("Allow collaboration");
    expect(list?.textContent).not.toContain("Edit profile");
    expect(list?.querySelectorAll("button")).toHaveLength(1);

    await act(async () => buttons().find((button) => button.textContent === "Manage")?.click());
    const management = document.querySelector('[aria-label="Manage employee: Researcher"]');
    expect(management?.textContent).toContain("Allow collaboration");

    await act(async () => buttons().find((button) => button.textContent === "Save permissions")?.click());
    expect(updatePolicy).toHaveBeenCalledWith("employee-a", current.collaboration_policy);

    await act(async () => buttons().find((button) => button.textContent === "Refresh sessions")?.click());
    expect(rollover).toHaveBeenCalledWith("employee-a");

    await act(async () => buttons().find((button) => button.textContent === "Suspend")?.click());
    expect(lifecycle).toHaveBeenCalledWith("employee-a", "suspended");

    await act(async () => buttons().find((button) => button.textContent === "Connect")?.click());
    const dialog = document.querySelector('[aria-label="Feishu / Lark binding"]');
    const textInputs = Array.from(dialog?.querySelectorAll<HTMLInputElement>("input") ?? [])
      .filter((input) => input.type === "text");
    const secret = dialog?.querySelector<HTMLInputElement>('input[type="password"]') ?? null;
    changeValue(textInputs[0] ?? null, "cli_app");
    changeValue(secret, "secret");
    await act(async () => {
      Array.from(dialog?.querySelectorAll<HTMLButtonElement>("button") ?? [])
        .find((button) => button.textContent === "Save")
        ?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(createBinding).toHaveBeenCalledWith("employee-a", expect.objectContaining({
      app_id: "cli_app",
      app_secret: "secret",
      domain: "feishu",
    }));
  });

  it("renders employee avatar URLs within the dashboard base path", async () => {
    const current = employee();
    current.avatar_url = "/api/employees/employee-a/avatar?v=123";
    vi.spyOn(api, "getEmployees").mockResolvedValue({ employees: [current] });

    await renderPane();

    expect(document.querySelector<HTMLImageElement>('[role="listitem"] img')?.getAttribute("src"))
      .toBe("/hermes/api/employees/employee-a/avatar?v=123");
  });

  it("reports refreshed employees to the chat workspace", async () => {
    const current = employee();
    const onEmployeesChanged = vi.fn();
    vi.spyOn(api, "getEmployees").mockResolvedValue({ employees: [current] });

    await renderPane("en", onEmployeesChanged);

    expect(onEmployeesChanged).toHaveBeenCalledWith([current]);
  });

  it("updates, tests, suspends, and revokes one existing binding", async () => {
    const current = employee({ feishu: binding() });
    vi.spyOn(api, "getEmployees").mockResolvedValue({ employees: [current] });
    const testBinding = vi.spyOn(api, "testEmployeeFeishuBinding").mockResolvedValue({
      bot_name: "Researcher Bot",
      ok: true,
      state: "connected",
    });
    const bindingLifecycle = vi.spyOn(api, "updateEmployeeFeishuBindingLifecycle").mockResolvedValue(current);
    const updateBinding = vi.spyOn(api, "updateEmployeeFeishuBinding").mockResolvedValue(current);

    await renderPane();
    const buttons = () => Array.from(document.querySelectorAll<HTMLButtonElement>("button"));
    await act(async () => buttons().find((button) => button.textContent === "Manage")?.click());
    await act(async () => buttons().find((button) => button.textContent === "Test connection")?.click());
    expect(testBinding).toHaveBeenCalledWith("employee-a");

    await act(async () => buttons().find((button) => button.textContent === "Suspend binding")?.click());
    expect(bindingLifecycle).toHaveBeenCalledWith("employee-a", "suspended");

    await act(async () => buttons().find((button) => button.textContent === "Revoke binding")?.click());
    expect(bindingLifecycle).toHaveBeenCalledWith("employee-a", "revoked");

    await act(async () => buttons().find((button) => button.textContent === "Update credentials")?.click());
    const dialog = document.querySelector('[aria-label="Feishu / Lark binding"]');
    const secret = dialog?.querySelector<HTMLInputElement>('input[type="password"]') ?? null;
    changeValue(secret, "new-secret");
    await act(async () => {
      Array.from(dialog?.querySelectorAll<HTMLButtonElement>("button") ?? [])
        .find((button) => button.textContent === "Save")
        ?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(updateBinding).toHaveBeenCalledWith("employee-a", expect.objectContaining({
      app_secret: "new-secret",
      expected_credential_version: 2,
    }));
  });
});

function binding() {
  return {
    connector_account_id: "account-a",
    app_id: "cli_app",
    binding_id: "binding-a",
    credential_version: 2,
    lifecycle_status: "active" as const,
    runtime_state: "running",
  };
}

function employee(channels: Employee["channels"] = {}): Employee {
  return {
    avatar_url: null,
    channels,
    collaboration_policy: {
      invite_quota: 5,
      may_create_groups: false,
      may_participate: true,
    },
    employee_id: "employee-a",
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
      toolsets: ["terminal"],
      workspace_relative_path: "employees/researcher",
    },
    profile_fingerprint: "sha256:test",
    profile_revision: 1,
  };
}
