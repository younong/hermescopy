// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Employee, type EmployeeCatalog } from "@/lib/api";
import { EmployeeManagementPane } from "./EmployeeManagementPane";

const catalog: EmployeeCatalog = {
  knowledge_roots: [],
  mcp_servers: [],
  model_registrations: [{ id: "model-a", name: "Model A" }],
  skills: [{ description: "Research", name: "research" }],
  toolsets: [{ description: "Terminal", name: "terminal" }],
  workspace: { default: "default", root: "" },
};

let root: Root | null = null;

async function renderPane() {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(<EmployeeManagementPane />);
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
    expect(dialog?.textContent).toContain("Add employee");
    expect(dialog?.textContent).not.toContain("App Secret");
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

    await act(async () => buttons().find((button) => button.textContent === "Save policy")?.click());
    expect(updatePolicy).toHaveBeenCalledWith("employee-a", current.collaboration_policy);

    await act(async () => buttons().find((button) => button.textContent === "Roll over sessions")?.click());
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
    await act(async () => buttons().find((button) => button.textContent === "Test")?.click());
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
