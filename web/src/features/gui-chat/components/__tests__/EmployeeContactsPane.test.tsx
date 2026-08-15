// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider, type Locale } from "@/i18n";
import { api, type Employee, type EmployeeCatalog, type EmployeeListResult } from "@/lib/api";
import { EmployeeContactsPane } from "../EmployeeContactsPane";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/api")>(),
  withHermesAssetAuth: (url: string) => `/hermes${url}`,
}));

const catalog: EmployeeCatalog = {
  knowledge_roots: [],
  mcp_servers: [],
  model_registrations: [{
    credential_configured: true,
    id: "model-a",
    kind: "chat",
    model: "claude-opus-4-8",
    mutable: true,
    name: "Anthropic",
    provider: "anthropic",
    reasoning_levels: ["high", "xhigh", "max"],
    scope: "user",
    source: "catalog",
    use_gateway: false,
  }],
  skills: [{ description: "Research", name: "research" }],
  toolsets: [{ description: "Terminal", name: "terminal" }],
  workspace: { default: "default", root: "" },
};

let root: Root | null = null;

function listResult(
  employees: Employee[],
  overrides: Partial<Omit<EmployeeListResult, "employees">> = {},
): EmployeeListResult {
  return {
    employees,
    page: 1,
    page_size: 20,
    total: employees.length,
    ...overrides,
  };
}

interface RenderPaneOptions {
  locale?: Locale;
  onEmployeeSelect?: (employeeId: string) => void;
  onRefresh?: () => void | Promise<void>;
  selectedEmployeeId?: string | null;
}

async function renderPane({
  locale = "en",
  onEmployeeSelect = vi.fn(),
  onRefresh = vi.fn(),
  selectedEmployeeId = null,
}: RenderPaneOptions = {}) {
  localStorage.setItem("hermes-locale", locale);
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(
      <I18nProvider>
        <EmployeeContactsPane
          onEmployeeSelect={onEmployeeSelect}
          onRefresh={onRefresh}
          selectedEmployeeId={selectedEmployeeId}
        />
      </I18nProvider>,
    );
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function flushSearchDebounce() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 400));
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

function buttonNamed(name: string) {
  return document.querySelector<HTMLButtonElement>(`button[aria-label="${name}"]`)
    ?? Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => button.textContent?.trim() === name)
    ?? null;
}

function pickerCheckbox(pickerId: string, name: string) {
  const picker = document.querySelector(`#${pickerId}`);
  const label = Array.from(picker?.querySelectorAll("label") ?? [])
    .find((item) => item.textContent?.trim() === name);
  return label?.querySelector("input") ?? null;
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  vi.spyOn(api, "getEmployeeCatalog").mockResolvedValue(catalog);
  vi.spyOn(api, "getEmployees").mockResolvedValue(listResult([]));
  document.body.innerHTML = "";
});

afterEach(async () => {
  await act(async () => root?.unmount());
  root = null;
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("EmployeeContactsPane", () => {
  it("fetches non-deleted contacts and renders them in Chinese", async () => {
    const getEmployees = vi.spyOn(api, "getEmployees").mockResolvedValue(listResult([employee()]));

    await renderPane({ locale: "zh", selectedEmployeeId: "employee-a" });

    expect(getEmployees).toHaveBeenCalledWith({
      page: 1,
      pageSize: 20,
      query: undefined,
      status: undefined,
    });
    expect(document.querySelector('[role="list"][aria-label="员工列表"]')).not.toBeNull();
    expect(document.body.textContent).toContain("通讯录");
    expect(document.body.textContent).toContain("选择联系人开始对话");
    expect(document.body.textContent).toContain("Analyst");
    expect(document.querySelector('[aria-current="true"]')?.textContent).toContain("Researcher");
  });

  it("selects contacts, keeps unavailable rows disabled, and searches via the API", async () => {
    const onEmployeeSelect = vi.fn();
    const unavailable = employee({
      employee_id: "employee-b",
      lifecycle_status: "suspended",
      name: "Writer",
      role: "Editor",
    });
    const getEmployees = vi.spyOn(api, "getEmployees")
      .mockResolvedValue(listResult([employee(), unavailable]));
    await renderPane({ onEmployeeSelect });

    const list = () => document.querySelector('[role="list"][aria-label="Employee list"]');
    const rows = Array.from(list()?.querySelectorAll<HTMLButtonElement>('li > button:not([aria-label])') ?? []);
    expect(rows).toHaveLength(2);
    expect(rows[1]?.getAttribute("aria-disabled")).toBe("true");
    expect(rows[1]?.textContent).toContain("Suspended");

    await act(async () => rows[0]?.click());
    expect(onEmployeeSelect).toHaveBeenCalledWith("employee-a");

    getEmployees.mockImplementation(async (options) =>
      options?.query === "writer" ? listResult([unavailable]) : listResult([]));
    changeValue(document.querySelector('input[aria-label="Search..."]'), "writer");
    await flushSearchDebounce();
    expect(getEmployees).toHaveBeenLastCalledWith(expect.objectContaining({
      page: 1,
      query: "writer",
    }));
    expect(list()?.querySelectorAll('[role="listitem"]')).toHaveLength(1);
    expect(list()?.textContent).toContain("Writer");

    changeValue(document.querySelector('input[aria-label="Search..."]'), "missing");
    await flushSearchDebounce();
    expect(document.body.textContent).toContain("No results");
  });

  it("filters by lifecycle status and paginates via the API", async () => {
    const getEmployees = vi.spyOn(api, "getEmployees").mockImplementation(async (options) => {
      if (options?.status === "active") return listResult([employee()]);
      return listResult(
        Array.from({ length: 20 }, (_, index) => employee({
          employee_id: `employee-${index}`,
          name: `Researcher ${index}`,
        })),
        { total: 25 },
      );
    });
    await renderPane();

    expect(document.body.textContent).toContain("Page 1 of 2");
    await act(async () => buttonNamed("Next page")?.click());
    expect(getEmployees).toHaveBeenLastCalledWith(expect.objectContaining({ page: 2 }));
    expect(buttonNamed("Previous page")?.disabled).toBe(false);

    const select = document.querySelector<HTMLSelectElement>('select[aria-label="Status"]');
    act(() => {
      const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
      setter?.call(select, "active");
      select?.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(getEmployees).toHaveBeenLastCalledWith(expect.objectContaining({
      page: 1,
      status: "active",
    }));
    expect(document.body.textContent).not.toContain("Page 1 of 2");
  });

  it("opens settings without selecting the contact", async () => {
    const onEmployeeSelect = vi.fn();
    vi.spyOn(api, "getEmployees").mockResolvedValue(listResult([employee()]));
    await renderPane({ onEmployeeSelect });

    await act(async () => buttonNamed("Manage employee: Researcher")?.click());

    expect(onEmployeeSelect).not.toHaveBeenCalled();
    expect(Array.from(document.querySelectorAll('[role="dialog"]')).find((dialog) => dialog.textContent?.includes("Manage the employee profile")))
      .not.toBeNull();
  });

  it("renders the built-in assistant without management actions", async () => {
    const builtin = employee({
      employee_id: "emp_builtin",
      employee_kind: "builtin_assistant",
      protected: true,
      profile: null,
    });
    const profileUpdate = vi.spyOn(api, "updateEmployeeProfile");
    const lifecycleUpdate = vi.spyOn(api, "updateEmployeeLifecycle");
    const collaborationUpdate = vi.spyOn(api, "updateEmployeeCollaborationPolicy");

    vi.spyOn(api, "getEmployees").mockResolvedValue(listResult([builtin]));
    await renderPane();

    expect(document.body.textContent).toContain("AI Assistant");
    expect(document.body.textContent).toContain("Built-in");
    expect(Array.from(document.querySelectorAll('[role="listitem"] span[aria-hidden]'))
      .some((avatar) => avatar.textContent === "AI")).toBe(true);
    await act(async () => buttonNamed("Manage employee: AI Assistant")?.click());

    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog?.textContent).toContain("managed by Hermes");
    for (const label of ["Edit profile", "Refresh sessions", "Suspend", "Revoke", "Save permissions", "Connect"]) {
      expect(Array.from(dialog?.querySelectorAll("button") ?? []).some((button) => button.textContent === label)).toBe(false);
    }
    expect(profileUpdate).not.toHaveBeenCalled();
    expect(lifecycleUpdate).not.toHaveBeenCalled();
    expect(collaborationUpdate).not.toHaveBeenCalled();
  });

  it("renders loading, error retry, and quiet empty states", async () => {
    const getEmployees = vi.spyOn(api, "getEmployees")
      .mockRejectedValueOnce(new Error("boom"))
      .mockResolvedValue(listResult([]));
    await renderPane();
    expect(document.querySelector('[role="alert"]')?.textContent).toContain("Could not load employees");
    await act(async () => {
      buttonNamed("Retry")?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(getEmployees).toHaveBeenCalledTimes(2);
    expect(document.body.textContent).not.toContain("No employees yet");
    expect(document.body.textContent).not.toContain("Add employees without connecting");

    let resolveList: ((result: EmployeeListResult) => void) | null = null;
    getEmployees.mockImplementation(
      () => new Promise<EmployeeListResult>((resolve) => { resolveList = resolve; }),
    );
    await act(async () => root?.unmount());
    root = null;
    document.body.innerHTML = "";
    const container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root?.render(
        <I18nProvider>
          <EmployeeContactsPane
            onEmployeeSelect={vi.fn()}
            onRefresh={vi.fn()}
            selectedEmployeeId={null}
          />
        </I18nProvider>,
      );
    });
    expect(document.querySelector('[role="status"]')?.textContent).toContain("Loading");
    await act(async () => {
      resolveList?.(listResult([]));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(document.querySelector('[role="status"]')).toBeNull();
  });

  it("creates an employee and asks the parent to refresh without Feishu credentials", async () => {
    const createEmployee = vi.spyOn(api, "createEmployee").mockResolvedValue(employee());
    const onRefresh = vi.fn();

    await renderPane({ onRefresh });
    await act(async () => buttonNamed("Add employee")?.click());

    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog?.textContent).toContain("Add employee");
    const selects = Array.from(dialog?.querySelectorAll<HTMLSelectElement>("select") ?? []);
    expect(selects[0]?.selectedOptions[0]?.textContent).toBe("claude-opus-4-8");
    expect(selects[1]?.selectedOptions[0]?.textContent).toBe("Default");
    expect(Array.from(selects[1]?.options ?? []).map((option) => option.textContent))
      .toEqual(["Default", "High", "XHigh", "Max"]);
    const modelReasoningRow = dialog?.querySelector<HTMLElement>(
      "[data-employee-model-reasoning-row]",
    );
    expect(modelReasoningRow?.children).toHaveLength(2);
    expect(modelReasoningRow?.className).toContain("sm:grid-cols-");
    expect(dialog?.textContent).not.toContain("Anthropic");
    expect(dialog?.textContent).not.toContain("App secret");
    const inputs = Array.from(dialog?.querySelectorAll<HTMLInputElement>("input") ?? [])
      .filter((input) => input.type === "text");
    changeValue(inputs[0] ?? null, "Researcher");
    act(() => {
      const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
      setter?.call(selects[1], "max");
      selects[1]?.dispatchEvent(new Event("change", { bubbles: true }));
    });
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
        reasoning_effort: "max",
        system_prompt: "Research carefully.",
        toolsets: ["terminal"],
      }),
    });
    expect(createEmployee.mock.calls[0]?.[0].profile).not.toHaveProperty(
      "workspace_relative_path",
    );
    expect(onRefresh).toHaveBeenCalledOnce();
  });

  it("hides reasoning levels when the selected model does not support them", async () => {
    vi.spyOn(api, "getEmployeeCatalog").mockResolvedValue({
      ...catalog,
      model_registrations: [{
        ...catalog.model_registrations[0],
        model: "claude-3-haiku",
        reasoning_levels: [],
      }],
    });

    await renderPane();
    await act(async () => buttonNamed("Add employee")?.click());

    const dialog = document.querySelector('[role="dialog"]');
    expect(dialog?.textContent).not.toContain("Reasoning level");
    expect(dialog?.querySelectorAll("select")).toHaveLength(1);
  });

  it("lets the owner pick toolsets on create and preserves saved toolsets on edit", async () => {
    vi.spyOn(api, "getEmployeeCatalog").mockResolvedValue({
      ...catalog,
      toolsets: [
        { description: "Terminal", name: "terminal" },
        { description: "Web research", name: "web" },
      ],
    });
    const createEmployee = vi.spyOn(api, "createEmployee").mockResolvedValue(employee());
    const updateProfile = vi.spyOn(api, "updateEmployeeProfile").mockResolvedValue(employee());

    vi.spyOn(api, "getEmployees").mockResolvedValue(listResult([employee()]));
    await renderPane({ onRefresh: vi.fn() });
    await act(async () => buttonNamed("Add employee")?.click());

    const createDialog = () => Array.from(document.querySelectorAll('[role="dialog"]'))
      .find((dialog) => dialog.textContent?.includes("Add employee"));
    expect(createDialog()?.textContent).toContain("Tools");
    expect(pickerCheckbox("employee-toolsets", "terminal")?.checked).toBe(true);
    expect(pickerCheckbox("employee-toolsets", "web")?.checked).toBe(true);
    await act(async () => pickerCheckbox("employee-toolsets", "web")?.click());
    expect(pickerCheckbox("employee-toolsets", "web")?.checked).toBe(false);

    const nameInput = Array.from(createDialog()?.querySelectorAll("input") ?? [])
      .find((input) => input.type === "text");
    changeValue(nameInput ?? null, "Researcher");
    changeValue(createDialog()?.querySelector("textarea") ?? null, "Research carefully.");
    await act(async () => {
      Array.from(createDialog()?.querySelectorAll("button") ?? [])
        .find((button) => button.textContent === "Save")
        ?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(createEmployee).toHaveBeenCalledWith({
      activate: true,
      profile: expect.objectContaining({ toolsets: ["terminal"] }),
    });

    await act(async () => buttonNamed("Manage employee: Researcher")?.click());
    await act(async () => Array.from(document.querySelectorAll("button"))
      .find((button) => button.textContent === "Edit profile")?.click());
    const editDialog = () => Array.from(document.querySelectorAll('[role="dialog"]'))
      .find((dialog) => dialog.textContent?.includes("Edit employee"));
    expect(pickerCheckbox("employee-toolsets", "terminal")?.checked).toBe(true);
    expect(pickerCheckbox("employee-toolsets", "web")?.checked).toBe(false);
    await act(async () => pickerCheckbox("employee-toolsets", "web")?.click());
    await act(async () => {
      Array.from(editDialog()?.querySelectorAll("button") ?? [])
        .find((button) => button.textContent === "Save")
        ?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(updateProfile).toHaveBeenCalledWith(
      "employee-a",
      expect.objectContaining({
        profile: expect.objectContaining({ toolsets: ["terminal", "web"] }),
      }),
    );
  });

  it("preserves profile, collaboration, lifecycle, rollover, and optional binding management", async () => {
    const current = employee();
    const onRefresh = vi.fn();
    const updateProfile = vi.spyOn(api, "updateEmployeeProfile").mockResolvedValue(current);
    const updatePolicy = vi.spyOn(api, "updateEmployeeCollaborationPolicy").mockResolvedValue(current);
    const rollover = vi.spyOn(api, "rolloverEmployeeSessions").mockResolvedValue({ ok: true, retired_sessions: 2 });
    const lifecycle = vi.spyOn(api, "updateEmployeeLifecycle").mockResolvedValue(current);
    const createBinding = vi.spyOn(api, "createEmployeeFeishuBinding").mockResolvedValue({
      ...current,
      channels: { feishu: binding() },
    });

    vi.spyOn(api, "getEmployees").mockResolvedValue(listResult([current]));
    await renderPane({ onRefresh });
    const buttons = () => Array.from(document.querySelectorAll<HTMLButtonElement>("button"));
    await act(async () => buttonNamed("Manage employee: Researcher")?.click());
    expect(Array.from(document.querySelectorAll('[role="dialog"]')).find((dialog) => dialog.textContent?.includes("Manage the employee profile"))?.textContent)
      .toContain("Allow collaboration");
    await act(async () => buttons().find((button) => button.textContent === "Edit profile")?.click());
    await act(async () => {
      Array.from(document.querySelectorAll<HTMLButtonElement>('[role="dialog"] button'))
        .find((button) => button.textContent === "Save")
        ?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(updateProfile).toHaveBeenCalledWith(
      "employee-a",
      expect.objectContaining({ expected_revision: 1 }),
    );
    expect(updateProfile.mock.calls[0]?.[1].profile).not.toHaveProperty(
      "workspace_relative_path",
    );

    await act(async () => buttonNamed("Manage employee: Researcher")?.click());
    await act(async () => buttons().find((button) => button.textContent === "Save permissions")?.click());
    expect(updatePolicy).toHaveBeenCalledWith("employee-a", current.collaboration_policy);
    await act(async () => buttons().find((button) => button.textContent === "Refresh sessions")?.click());
    expect(rollover).toHaveBeenCalledWith("employee-a");
    await act(async () => buttons().find((button) => button.textContent === "Suspend")?.click());
    expect(lifecycle).toHaveBeenCalledWith("employee-a", "suspended");

    await act(async () => buttons().find((button) => button.textContent === "Connect")?.click());
    const dialog = Array.from(document.querySelectorAll('[role="dialog"]')).find((dialog) => dialog.textContent?.includes("Optional. Each employee"));
    const textInputs = Array.from(dialog?.querySelectorAll<HTMLInputElement>("input") ?? [])
      .filter((input) => input.type === "text");
    changeValue(textInputs[0] ?? null, "cli_app");
    changeValue(dialog?.querySelector('input[type="password"]') ?? null, "secret");
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
    expect(onRefresh).toHaveBeenCalledTimes(5);
  });

  it("renders authenticated avatars and manages an existing binding", async () => {
    const current = employee({ avatar_url: "/api/employees/employee-a/avatar?v=123", channels: { feishu: binding() } });
    const onRefresh = vi.fn();
    const testBinding = vi.spyOn(api, "testEmployeeFeishuBinding").mockResolvedValue({
      bot_name: "Researcher Bot",
      ok: true,
      state: "connected",
    });
    const bindingLifecycle = vi.spyOn(api, "updateEmployeeFeishuBindingLifecycle").mockResolvedValue(current);
    const updateBinding = vi.spyOn(api, "updateEmployeeFeishuBinding").mockResolvedValue(current);

    vi.spyOn(api, "getEmployees").mockResolvedValue(listResult([current]));
    await renderPane({ onRefresh });
    expect(document.querySelector<HTMLImageElement>('[role="listitem"] img')?.getAttribute("src"))
      .toBe("/hermes/api/employees/employee-a/avatar?v=123");

    const buttons = () => Array.from(document.querySelectorAll<HTMLButtonElement>("button"));
    await act(async () => buttonNamed("Manage employee: Researcher")?.click());
    await act(async () => buttons().find((button) => button.textContent === "Test connection")?.click());
    expect(testBinding).toHaveBeenCalledWith("employee-a");
    await act(async () => buttons().find((button) => button.textContent === "Suspend binding")?.click());
    expect(bindingLifecycle).toHaveBeenCalledWith("employee-a", "suspended");
    await act(async () => buttons().find((button) => button.textContent === "Revoke binding")?.click());
    expect(bindingLifecycle).toHaveBeenCalledWith("employee-a", "revoked");

    await act(async () => buttons().find((button) => button.textContent === "Update credentials")?.click());
    const dialog = Array.from(document.querySelectorAll('[role="dialog"]')).find((dialog) => dialog.textContent?.includes("Optional. Each employee"));
    changeValue(dialog?.querySelector('input[type="password"]') ?? null, "new-secret");
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

function employee(overrides: {
  avatar_url?: string | null;
  channels?: Employee["channels"];
  employee_id?: string;
  employee_kind?: Employee["employee_kind"];
  lifecycle_status?: Employee["lifecycle_status"];
  name?: string;
  profile?: Employee["profile"];
  protected?: boolean;
  role?: string;
} = {}): Employee {
  const profile: Employee["profile"] = "profile" in overrides ? overrides.profile ?? null : {
    knowledge_relative_paths: [],
    max_iterations: 20,
    max_tokens: null,
    mcp_servers: [],
    model_registration_id: "model-a",
    name: overrides.name ?? "Researcher",
    reasoning_effort: "high" as const,
    role: overrides.role ?? "Analyst",
    schema_version: 1 as const,
    skills: [],
    system_prompt: "Research carefully.",
    toolsets: ["terminal"],
    workspace_relative_path: "employees/researcher",
  };
  return {
    avatar_url: overrides.avatar_url ?? null,
    channels: overrides.channels ?? {},
    chat_eligible: (overrides.lifecycle_status ?? "active") === "active",
    employee_kind: overrides.employee_kind ?? "managed",
    protected: overrides.protected ?? false,
    collaboration_policy: {
      invite_quota: 5,
      may_create_groups: false,
      may_participate: true,
    },
    employee_id: overrides.employee_id ?? "employee-a",
    lifecycle_status: overrides.lifecycle_status ?? "active",
    profile,
    profile_fingerprint: profile ? "sha256:test" : null,
    profile_revision: profile ? 1 : null,
  };
}
