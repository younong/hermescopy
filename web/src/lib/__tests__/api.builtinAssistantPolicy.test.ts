// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  api,
  employeeDisplayName,
  employeeDisplayRole,
  type Employee,
  type UpdateBuiltinAssistantPolicyRequest,
} from "../api";

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("built-in assistant policy API", () => {
  it("uses personalized and fallback employee display names", () => {
    expect(employeeDisplayName(builtinEmployee("Nova"), "AI Assistant", "Unnamed"))
      .toBe("Nova");
    expect(employeeDisplayName(builtinEmployee("  "), "AI Assistant", "Unnamed"))
      .toBe("AI Assistant");
    expect(employeeDisplayName(managedEmployee("Researcher"), "AI Assistant", "Unnamed"))
      .toBe("Researcher");
    expect(employeeDisplayName(managedEmployee(""), "AI Assistant", "Unnamed"))
      .toBe("Unnamed");
    expect(employeeDisplayRole(builtinEmployee("Nova"), "Built-in", "AI employee"))
      .toBe("Built-in");
    expect(employeeDisplayRole(managedEmployee("Researcher"), "Built-in", "AI employee"))
      .toBe("Analyst");
  });

  it("uses the system policy contract for reads and updates", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.resolve(response({ policy: null, admin_chat_registrations: [] })),
    );
    const request: UpdateBuiltinAssistantPolicyRequest = {
      model_registration_id: "admin-chat-a",
      reasoning_effort: "max",
      expected_revision: 0,
    };

    await api.getBuiltinAssistantPolicy();
    await api.updateBuiltinAssistantPolicy(request);

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/system/builtin-assistant-policy",
      "/api/system/builtin-assistant-policy",
    ]);
    expect(fetchMock.mock.calls[1]?.[1]).toMatchObject({
      method: "PUT",
      body: JSON.stringify(request),
    });
  });

  it("uses the dedicated built-in personalization contract", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.resolve(response(builtinEmployee("Nova"))),
    );
    const request = {
      expected_revision: 3,
      nickname: "Nova",
      personal_preference: "Prefer concise answers.",
    };

    await api.updateBuiltinAssistantPersonalization("employee/a", request);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/employees/employee%2Fa/builtin-assistant-personalization",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify(request),
      }),
    );
  });
});

function employeeBase() {
  return {
    avatar_url: null,
    channels: {},
    chat_eligible: true,
    collaboration_policy: {
      invite_quota: null,
      may_create_groups: true,
      may_participate: true,
    },
    employee_id: "employee-a",
    lifecycle_status: "active" as const,
    profile_fingerprint: "sha256:test",
    profile_revision: 3,
    protected: false,
  };
}

function builtinEmployee(
  nickname: string,
): Extract<Employee, { employee_kind: "builtin_assistant" }> {
  return {
    ...employeeBase(),
    builtin_assistant_personalization: {
      nickname,
      personal_preference: "",
    },
    employee_kind: "builtin_assistant",
    profile: null,
    protected: true,
  };
}

function managedEmployee(
  name: string,
): Extract<Employee, { employee_kind: "managed" }> {
  return {
    ...employeeBase(),
    employee_kind: "managed",
    profile: {
      knowledge_relative_paths: [],
      max_iterations: 20,
      mcp_servers: [],
      model_registration_id: "chat-a",
      name,
      role: "Analyst",
      schema_version: 1,
      skills: [],
      system_prompt: "Help the owner.",
      toolsets: [],
      workspace_relative_path: "employees/employee-a",
    },
  };
}
