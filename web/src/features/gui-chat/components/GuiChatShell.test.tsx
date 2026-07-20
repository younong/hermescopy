// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { GuiChatConnection } from "../api";
import {
  DashboardAuthIdentityProvider,
  useDashboardAuthIdentity,
} from "@/lib/useDashboardAuthIdentity";
import { dashboardAuthTransition } from "@/lib/dashboardAuthTransition";
import { GuiChatShell } from "./GuiChatShell";

const mocks = vi.hoisted(() => ({
  connectGuiChat: vi.fn(),
  getAuthMe: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, getAuthMe: mocks.getAuthMe },
  };
});

vi.mock("../api", () => ({
  connectGuiChat: mocks.connectGuiChat,
}));

vi.mock("../mock", () => ({
  connectMockGuiChat: vi.fn(),
}));

vi.mock("@/contexts/usePageHeader", () => ({
  usePageHeader: () => ({ setEnd: vi.fn(), setTitle: vi.fn() }),
}));

vi.mock("@/contexts/useProfileScope", () => ({
  useProfileScope: () => ({ profile: "" }),
}));

vi.mock("@/i18n", () => ({
  useI18n: () => ({
    t: { common: { retry: "Retry" }, sessions: { title: "Sessions" } },
  }),
}));

vi.mock("@/components/ChatSessionList", () => ({
  ChatSessionList: () => null,
}));

vi.mock("./Composer", () => ({
  Composer: () => null,
}));

vi.mock("./MessageList", () => ({
  MessageList: () => null,
}));

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  mocks.connectGuiChat.mockReset();
  mocks.getAuthMe.mockReset();
  window.__HERMES_AUTH_REQUIRED__ = true;
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    addEventListener: vi.fn(),
    matches: false,
    media: query,
    removeEventListener: vi.fn(),
  }));
  document.body.innerHTML = "";
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  dashboardAuthTransition.reset();
  delete window.__HERMES_AUTH_REQUIRED__;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("GuiChatShell", () => {
  it("connects automatically when the authenticated owner becomes ready", async () => {
    const identity = deferred<AuthIdentity>();
    const firstConnection = createConnection();
    const connection = createConnection();
    mocks.getAuthMe.mockReturnValue(identity.promise);
    mocks.connectGuiChat
      .mockReturnValueOnce(firstConnection)
      .mockReturnValueOnce(connection);

    const container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => {
      root?.render(
        <MemoryRouter initialEntries={["/chat-gui"]}>
          <DashboardAuthIdentityProvider>
            <ReadyProbe />
            <GuiChatShell />
          </DashboardAuthIdentityProvider>
        </MemoryRouter>,
      );
    });

    expect(connection.createOrAttach).not.toHaveBeenCalled();

    await act(async () => {
      identity.resolve(authIdentity());
      await identity.promise;
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(mocks.connectGuiChat).toHaveBeenCalledTimes(2);
    expect(mocks.connectGuiChat.mock.calls).toEqual([
      [{ ownerKey: undefined, profile: "" }],
      [{ ownerKey: "owner-a", profile: "" }],
    ]);
    expect(document.querySelector("[data-ready]")?.outerHTML).toContain('data-ready="true"');
    expect(firstConnection.close).toHaveBeenCalledOnce();
    expect(document.body.textContent).toContain("idle");
    expect(connection.createOrAttach).toHaveBeenCalledOnce();
    expect(connection.createOrAttach).toHaveBeenCalledWith(
      null,
      expect.any(Number),
      expect.any(AbortSignal),
      undefined,
    );
  });

  it("connects once when loading a resumed route", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    const container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root?.render(
        <MemoryRouter initialEntries={["/chat-gui?resume=requested"]}>
          <DashboardAuthIdentityProvider>
            <GuiChatShell />
          </DashboardAuthIdentityProvider>
        </MemoryRouter>,
      );
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(connection.createOrAttach).toHaveBeenCalledOnce();
  });
});

function ReadyProbe() {
  const { ready } = useDashboardAuthIdentity();
  return <span data-ready={ready} />;
}

function createConnection(): GuiChatConnection {
  const eventHandlers = new Set<(event: never) => void>();
  const stateHandlers = new Set<(state: "idle") => void>();
  return {
    attachFile: vi.fn(),
    attachImage: vi.fn(),
    attachPdf: vi.fn(),
    client: {
      onEvent: (handler) => {
        eventHandlers.add(handler);
        return () => eventHandlers.delete(handler);
      },
      onState: (handler) => {
        stateHandlers.add(handler);
        handler("idle");
        return () => stateHandlers.delete(handler);
      },
    },
    close: vi.fn(),
    createOrAttach: vi.fn().mockResolvedValue({
      cwd: "/tmp",
      model: "test-model",
      session_id: "runtime-a",
      stored_session_id: "stored-a",
    }),
    respondToApproval: vi.fn(),
    send: vi.fn(),
    stop: vi.fn(),
  };
}

interface AuthIdentity {
  display_name: string;
  email: string;
  expires_at: number;
  org_id: string;
  owner_key: string;
  provider: string;
  tenant_id: string;
  user_id: string;
}

function authIdentity(): AuthIdentity {
  return {
    display_name: "",
    email: "",
    expires_at: 4_102_444_800,
    org_id: "org-a",
    owner_key: "owner-a",
    provider: "local",
    tenant_id: "tenant-a",
    user_id: "user-a",
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}
