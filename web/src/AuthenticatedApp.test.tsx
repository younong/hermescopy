// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AuthMeResponse } from "@/lib/api";
import AuthenticatedApp from "./AuthenticatedApp";

const mocks = vi.hoisted(() => ({
  identity: vi.fn(),
}));

vi.mock("@/lib/useDashboardAuthIdentity", () => ({
  useDashboardAuthIdentity: mocks.identity,
}));

vi.mock("@/App", () => ({
  default: () => <div data-admin-app>Admin dashboard</div>,
}));

vi.mock("@/contexts/SystemActions", () => ({
  SystemActionsProvider: ({ children }: { children: React.ReactNode }) => (
    <div data-system-actions>{children}</div>
  ),
}));

vi.mock("@/components/ForcedPasswordChangePage", () => ({
  ForcedPasswordChangePage: () => <div data-forced-password>Change password</div>,
}));

vi.mock("@/pages/StandaloneGuiChatPage", () => ({
  default: MemberChatProbe,
}));

function MemberChatProbe() {
  const location = useLocation();
  return (
    <div
      data-member-chat
      data-pathname={location.pathname}
      data-search={location.search}
    >
      Member chat
    </div>
  );
}

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  mocks.identity.mockReset();
  document.body.innerHTML = "";
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("AuthenticatedApp", () => {
  it("waits for identity before mounting either application", () => {
    mocks.identity.mockReturnValue(identity({ loading: true }));

    renderApp("/");

    expect(document.querySelector("[data-admin-app]")).toBeNull();
    expect(document.querySelector("[data-member-chat]")).toBeNull();
    expect(document.querySelector('[aria-busy="true"]')).not.toBeNull();
  });

  it("shows authentication failures before selecting an application", () => {
    mocks.identity.mockReturnValue(identity({ error: "network unavailable" }));

    renderApp("/");

    expect(document.body.textContent).toContain("Authentication unavailable");
    expect(document.querySelector("[data-admin-app]")).toBeNull();
    expect(document.querySelector("[data-member-chat]")).toBeNull();
  });

  it("requires a password change before member chat", () => {
    mocks.identity.mockReturnValue(
      identity({ authMe: authMe("member", { must_change_password: true }) }),
    );

    renderApp("/");

    expect(document.querySelector("[data-forced-password]")).not.toBeNull();
    expect(document.querySelector("[data-member-chat]")).toBeNull();
  });

  it.each(["/", "/sessions", "/system", "/chat", "/plugin-page"])(
    "redirects a member from %s to standalone chat",
    async (entry) => {
      mocks.identity.mockReturnValue(identity({ authMe: authMe("member") }));

      renderApp(entry);
      await flush();

      expect(document.querySelector("[data-member-chat]")?.getAttribute("data-pathname")).toBe(
        "/chat-gui",
      );
      expect(document.querySelector("[data-admin-app]")).toBeNull();
      expect(document.querySelector("[data-system-actions]")).toBeNull();
    },
  );

  it("preserves a member chat resume deep link", async () => {
    mocks.identity.mockReturnValue(identity({ authMe: authMe("member") }));

    renderApp("/chat-gui?resume=session-a");
    await flush();

    const chat = document.querySelector("[data-member-chat]");
    expect(chat?.getAttribute("data-pathname")).toBe("/chat-gui");
    expect(chat?.getAttribute("data-search")).toBe("?resume=session-a");
  });

  it("keeps administrators and role-less authenticated users on the dashboard", () => {
    for (const me of [authMe("admin"), authMe(undefined)]) {
      mocks.identity.mockReturnValue(identity({ authMe: me }));
      renderApp("/");
      expect(document.querySelector("[data-admin-app]")).not.toBeNull();
      expect(document.querySelector("[data-member-chat]")).toBeNull();
      act(() => root?.unmount());
      root = null;
      document.body.innerHTML = "";
    }
  });

  it("keeps auth-disabled loopback mode on the dashboard", () => {
    mocks.identity.mockReturnValue(
      identity({ authMe: null, authRequired: false, ownerKey: undefined, ready: true }),
    );

    renderApp("/");

    expect(document.querySelector("[data-admin-app]")).not.toBeNull();
    expect(document.querySelector("[data-member-chat]")).toBeNull();
  });

  it("honors a router basename for member redirects", async () => {
    mocks.identity.mockReturnValue(identity({ authMe: authMe("member") }));

    renderApp("/hermes/sessions", "/hermes");
    await flush();

    expect(document.querySelector("[data-member-chat]")?.getAttribute("data-pathname")).toBe(
      "/chat-gui",
    );
  });
});

function renderApp(entry: string, basename?: string) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <MemoryRouter basename={basename} initialEntries={[entry]}>
        <AuthenticatedApp />
      </MemoryRouter>,
    );
  });
}

async function flush() {
  await act(async () => {
    await Promise.resolve();
  });
}

function authMe(
  role?: AuthMeResponse["role"],
  overrides: Partial<AuthMeResponse> = {},
): AuthMeResponse {
  return {
    display_name: "Test User",
    email: "test@example.com",
    expires_at: 4_102_444_800,
    org_id: "org-a",
    owner_key: "owner-a",
    provider: "basic",
    role,
    tenant_id: "tenant-a",
    user_id: "user-a",
    ...overrides,
  };
}

function identity(overrides: Record<string, unknown> = {}) {
  return {
    authMe: authMe("admin"),
    authRequired: true,
    error: null,
    loading: false,
    ownerKey: "owner-a",
    ready: true,
    refresh: vi.fn(),
    ...overrides,
  };
}
