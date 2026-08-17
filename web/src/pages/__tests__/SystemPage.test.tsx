// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type AuthMeResponse, type BuiltinAssistantPolicyResponse } from "@/lib/api";
import SystemPage from "../SystemPage";

let root: Root | null = null;

const registration = {
  id: "admin-chat-a",
  name: "Deployment chat",
  kind: "chat" as const,
  provider: "deployment-provider",
  model: "deployment-model",
  source: "catalog" as const,
  scope: "admin" as const,
  mutable: false,
  use_gateway: false,
  credential_configured: null,
};

function authMe(isAdmin: boolean): AuthMeResponse {
  return {
    user_id: "account-a",
    email: "admin@example.com",
    display_name: "Admin",
    org_id: "local",
    tenant_id: "local",
    owner_key: "local",
    provider: "local_basic",
    expires_at: 0,
    local_user_management: { enabled: true, is_admin: isAdmin },
  };
}

function policy(revision: number | null): BuiltinAssistantPolicyResponse {
  return {
    policy: revision === null ? null : {
      model_registration_id: registration.id,
      reasoning_effort: "high",
      revision,
      updated_at: 1,
    },
    admin_chat_registrations: [registration],
  };
}

function mockBaseApi(identity: AuthMeResponse) {
  vi.spyOn(api, "getStatus").mockResolvedValue(null as never);
  vi.spyOn(api, "getSystemStats").mockResolvedValue(null as never);
  vi.spyOn(api, "getMemory").mockResolvedValue({
    active: null,
    builtin_files: { memory: 0, user: 0 },
  } as never);
  vi.spyOn(api, "getCredentialPool").mockResolvedValue({ providers: [] });
  vi.spyOn(api, "getCheckpoints").mockResolvedValue({ sessions: [], total_bytes: 0 } as never);
  vi.spyOn(api, "getHooks").mockResolvedValue({ hooks: [], valid_events: [] });
  vi.spyOn(api, "getCurator").mockResolvedValue(null as never);
  vi.spyOn(api, "getPortal").mockResolvedValue(null as never);
  vi.spyOn(api, "getAuthMe").mockResolvedValue(identity);
  vi.spyOn(api, "checkHermesUpdate").mockResolvedValue({} as never);
  vi.spyOn(api, "getLocalUsers").mockResolvedValue({ accounts: [], count: 0, max_accounts: 20 });
}

async function renderPage() {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(<MemoryRouter><SystemPage /></MemoryRouter>);
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
  return container;
}

function saveButton(container: HTMLElement): HTMLButtonElement {
  const button = Array.from(container.querySelectorAll("button")).find(
    (candidate) => candidate.textContent?.trim() === "Save policy",
  );
  if (!button) throw new Error("Save policy button not found");
  return button;
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

describe("SystemPage built-in assistant policy", () => {
  it("fetches and shows the policy only for local administrators", async () => {
    mockBaseApi(authMe(false));
    const getPolicy = vi.spyOn(api, "getBuiltinAssistantPolicy");

    const memberPage = await renderPage();

    expect(getPolicy).not.toHaveBeenCalled();
    expect(memberPage.textContent).not.toContain("Global conversation policy");

    await act(async () => root?.unmount());
    root = null;
    document.body.innerHTML = "";
    vi.restoreAllMocks();

    mockBaseApi(authMe(true));
    const adminGetPolicy = vi.spyOn(api, "getBuiltinAssistantPolicy").mockResolvedValue(policy(null));

    const adminPage = await renderPage();

    expect(adminGetPolicy).toHaveBeenCalledOnce();
    expect(adminPage.textContent).toContain("Global conversation policy");
    expect(adminPage.textContent).toContain("all users' new or reconstructed built-in assistant conversations");
    expect(adminPage.textContent).toContain("Existing conversations are unchanged");
    expect(adminPage.textContent).toContain("ordinary chat model selection is unaffected");
  });

  it("uses revision zero initially and keeps the successful response authoritative", async () => {
    mockBaseApi(authMe(true));
    vi.spyOn(api, "getBuiltinAssistantPolicy").mockResolvedValue(policy(null));
    const updatePolicy = vi.spyOn(api, "updateBuiltinAssistantPolicy")
      .mockResolvedValueOnce(policy(1))
      .mockResolvedValueOnce(policy(2));
    const container = await renderPage();

    await act(async () => saveButton(container).click());
    await act(async () => saveButton(container).click());

    expect(updatePolicy).toHaveBeenNthCalledWith(1, {
      model_registration_id: registration.id,
      reasoning_effort: "high",
      expected_revision: 0,
    });
    expect(updatePolicy).toHaveBeenNthCalledWith(2, {
      model_registration_id: registration.id,
      reasoning_effort: "high",
      expected_revision: 1,
    });
  });

  it("reloads the authoritative policy after an update error", async () => {
    mockBaseApi(authMe(true));
    const getPolicy = vi.spyOn(api, "getBuiltinAssistantPolicy")
      .mockResolvedValueOnce(policy(2))
      .mockResolvedValueOnce(policy(3));
    vi.spyOn(api, "updateBuiltinAssistantPolicy").mockRejectedValue(new Error("conflict"));
    const container = await renderPage();

    await act(async () => saveButton(container).click());

    expect(getPolicy).toHaveBeenCalledTimes(2);
    expect(document.body.textContent).toContain("Built-in assistant policy update failed");
  });
});
