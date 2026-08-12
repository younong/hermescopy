// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Employee, type EmployeeCatalog } from "@/lib/api";
import { EmployeeManagementPane } from "./EmployeeManagementPane";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/api")>(),
  withHermesAssetAuth: (url: string) => `/hermes${url}`,
}));

const catalog: EmployeeCatalog = {
  knowledge_roots: [],
  mcp_servers: [],
  model_registrations: [{ id: "model-a", name: "Model A" }],
  skills: [{ description: "Research", name: "research" }],
  toolsets: [{ description: "Terminal", name: "terminal" }],
  workspace: { default: "default", root: "" },
};

let root: Root | null = null;

async function renderPane(onEmployeesChanged?: (employees: Employee[]) => void) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(<EmployeeManagementPane onEmployeesChanged={onEmployeesChanged} />);
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
  it("creates an employee without requesting Feishu credentials", async () => {
    vi.spyOn(api, "getEmployees").mockResolvedValue({ employees: [] });
    const createEmployee = vi.spyOn(api, "createEmployee").mockResolvedValue(employee());

    await renderPane();
    await act(async () => document.querySelector<HTMLButtonElement>("button.gui-chat-workspace-primary-button")?.click());

    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog?.textContent).toContain("添加员工");
    expect(dialog?.textContent).not.toContain("应用密钥");
    const inputs = Array.from(dialog?.querySelectorAll<HTMLInputElement>("input") ?? [])
      .filter((input) => input.type === "text");
    changeValue(inputs[0] ?? null, "Researcher");
    changeValue(dialog?.querySelector("textarea") ?? null, "Research carefully.");
    const save = Array.from(dialog?.querySelectorAll<HTMLButtonElement>("button") ?? [])
      .find((button) => button.textContent === "保存");
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
    const list = document.querySelector('[role="list"][aria-label="员工列表"]');
    expect(list?.querySelectorAll('[role="listitem"]')).toHaveLength(1);
    expect(document.body.textContent).toContain("员工");
    expect(document.body.textContent).not.toContain("Employees");
    expect(list?.textContent).not.toContain("允许参与协作");
    expect(list?.textContent).not.toContain("编辑资料");
    expect(list?.querySelectorAll("button")).toHaveLength(1);

    await act(async () => buttons().find((button) => button.textContent === "管理")?.click());
    const management = document.querySelector('[aria-label="管理员工：Researcher"]');
    expect(management?.textContent).toContain("允许参与协作");

    await act(async () => buttons().find((button) => button.textContent === "保存权限")?.click());
    expect(updatePolicy).toHaveBeenCalledWith("employee-a", current.collaboration_policy);

    await act(async () => buttons().find((button) => button.textContent === "更新会话")?.click());
    expect(rollover).toHaveBeenCalledWith("employee-a");

    await act(async () => buttons().find((button) => button.textContent === "暂停")?.click());
    expect(lifecycle).toHaveBeenCalledWith("employee-a", "suspended");

    await act(async () => buttons().find((button) => button.textContent === "连接")?.click());
    const dialog = document.querySelector('[aria-label="飞书 / Lark 绑定"]');
    const textInputs = Array.from(dialog?.querySelectorAll<HTMLInputElement>("input") ?? [])
      .filter((input) => input.type === "text");
    const secret = dialog?.querySelector<HTMLInputElement>('input[type="password"]') ?? null;
    changeValue(textInputs[0] ?? null, "cli_app");
    changeValue(secret, "secret");
    await act(async () => {
      Array.from(dialog?.querySelectorAll<HTMLButtonElement>("button") ?? [])
        .find((button) => button.textContent === "保存")
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

    await renderPane(onEmployeesChanged);

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
    await act(async () => buttons().find((button) => button.textContent === "管理")?.click());
    await act(async () => buttons().find((button) => button.textContent === "测试连接")?.click());
    expect(testBinding).toHaveBeenCalledWith("employee-a");

    await act(async () => buttons().find((button) => button.textContent === "暂停绑定")?.click());
    expect(bindingLifecycle).toHaveBeenCalledWith("employee-a", "suspended");

    await act(async () => buttons().find((button) => button.textContent === "撤销绑定")?.click());
    expect(bindingLifecycle).toHaveBeenCalledWith("employee-a", "revoked");

    await act(async () => buttons().find((button) => button.textContent === "更新凭据")?.click());
    const dialog = document.querySelector('[aria-label="飞书 / Lark 绑定"]');
    const secret = dialog?.querySelector<HTMLInputElement>('input[type="password"]') ?? null;
    changeValue(secret, "new-secret");
    await act(async () => {
      Array.from(dialog?.querySelectorAll<HTMLButtonElement>("button") ?? [])
        .find((button) => button.textContent === "保存")
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
