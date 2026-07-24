// @vitest-environment jsdom

import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { GuiChatConnection } from "../api";
import type { ConnectionState } from "@/lib/gatewayClient";
import {
  DashboardAuthIdentityProvider,
  useDashboardAuthIdentity,
} from "@/lib/useDashboardAuthIdentity";
import { dashboardAuthTransition } from "@/lib/dashboardAuthTransition";
import { GuiChatShell } from "./GuiChatShell";

const mocks = vi.hoisted(() => ({
  connectGuiChat: vi.fn(),
  createILinkEnrollment: vi.fn(),
  getAuthMe: vi.fn(),
  getILinkEnrollment: vi.fn(),
  logout: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      createILinkEnrollment: mocks.createILinkEnrollment,
      getAuthMe: mocks.getAuthMe,
      getILinkEnrollment: mocks.getILinkEnrollment,
      logout: mocks.logout,
    },
  };
});

vi.mock("../api", () => ({
  connectGuiChat: mocks.connectGuiChat,
}));

vi.mock("../mock", () => ({
  connectMockGuiChat: vi.fn(),
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
  Composer: (props: Record<string, unknown>) => (
    <button
      data-composer-send
      onClick={() =>
        void (props.onSend as (...args: unknown[]) => unknown)(
          "new message",
          [],
          () => undefined,
        )
      }
    >
      Composer send
    </button>
  ),
}));

vi.mock("./MessageList", () => ({
  MessageList: (props: Record<string, unknown>) => (
    <button
      data-clarify-answer
      onClick={() =>
        (props.onClarifyRespond as (...args: unknown[]) => unknown)("clarify-1", "A")
      }
    >
      Clarify answer
    </button>
  ),
}));

vi.mock("@/features/files/components/GuiChatFilesPane", () => ({
  GuiChatFilesPane: () => <section data-files-pane>Files pane</section>,
}));

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  mocks.connectGuiChat.mockReset();
  mocks.createILinkEnrollment.mockReset();
  mocks.getAuthMe.mockReset();
  mocks.getILinkEnrollment.mockReset();
  mocks.logout.mockReset();
  mocks.logout.mockResolvedValue(new Response(null, { status: 200 }));
  mocks.createILinkEnrollment.mockResolvedValue({
    attempt_id: "enr_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    qr_content: "https://example.invalid/qr",
    status: "waiting",
    expires_at: 123,
  });
  mocks.getILinkEnrollment.mockResolvedValue({
    status: "confirmed",
    expires_at: 123,
    next_action: "continue_in_wechat",
  });
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
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("GuiChatShell", () => {
  it("renders the dedicated workspace navigation", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);

    expect(document.querySelector("[data-gui-chat]")).not.toBeNull();
    expect(document.body.textContent).not.toContain("Terminal chat");
    expect(document.querySelector<HTMLButtonElement>('button[aria-current="page"]')?.textContent).toContain("New chat");
    expect(document.querySelector('aside[aria-label="Chat workspace"]')).not.toBeNull();
    expect(document.querySelector('[aria-label="Log out"]')).not.toBeNull();
  });

  it("opens files inside the dedicated workspace and returns to chat", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);
    await act(async () => {
      Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
        .find((button) => button.textContent?.includes("Files"))
        ?.click();
      await Promise.resolve();
    });

    expect(document.querySelector('aside[aria-label="Chat workspace"]')).not.toBeNull();
    expect(document.querySelector("[data-files-pane]")).not.toBeNull();
    expect(document.querySelector("[data-composer-send]")).toBeNull();
    expect(Array.from(document.querySelectorAll<HTMLButtonElement>('button[aria-current="page"]'))
      .some((button) => button.textContent?.includes("Files"))).toBe(true);
    expect(connection.createOrAttach).toHaveBeenCalledTimes(1);

    await act(async () => {
      Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
        .find((button) => button.textContent?.includes("New chat"))
        ?.click();
      await Promise.resolve();
    });

    expect(document.querySelector("[data-files-pane]")).toBeNull();
    expect(document.querySelector("[data-composer-send]")).not.toBeNull();
    expect(connection.createOrAttach).toHaveBeenCalledTimes(2);
  });

  it("shows the WeChat action only when the authenticated connector is ready", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue({
      ...authIdentity(),
      features: { weixin_ilink_connect: true },
    });
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);

    const connect = document.querySelector<HTMLButtonElement>('[aria-label="Connect WeChat"]');
    expect(connect).not.toBeNull();
    await act(async () => {
      connect?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(document.querySelector('[role="dialog"]')).not.toBeNull();
    expect(mocks.createILinkEnrollment).toHaveBeenCalledOnce();
  });

  it("hides the WeChat action when the connector feature is unavailable", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);

    expect(document.querySelector('[aria-label="Connect WeChat"]')).toBeNull();
  });

  it("logs out from the dedicated workspace", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);
    await act(async () => {
      document.querySelector<HTMLButtonElement>('[aria-label="Log out"]')?.click();
      await Promise.resolve();
    });

    expect(mocks.logout).toHaveBeenCalledOnce();
  });

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
    expect(document.querySelector("[data-gui-chat]")).not.toBeNull();
    expect(document.querySelector('aside[aria-label="Chat workspace"]')).not.toBeNull();
    expect(document.body.textContent).toContain("idle");
    expect(connection.createOrAttach).toHaveBeenCalledOnce();
    expect(connection.createOrAttach).toHaveBeenCalledWith(
      null,
      expect.any(Number),
      expect.any(AbortSignal),
      undefined,
    );
  });

  it("responds to clarify with the owning runtime session", async () => {
    const connection = createConnection()
    connection.createOrAttachMock.mockResolvedValue({
      pending_prompts: [
        {
          choices: ["A"],
          question: "Pick one",
          request_id: "clarify-1",
          type: "clarify",
        },
      ],
      session_id: "runtime-a",
      stored_session_id: "stored-a",
    })
    mocks.getAuthMe.mockResolvedValue(authIdentity())
    mocks.connectGuiChat.mockReturnValue(connection)

    const container = document.createElement("div")
    document.body.appendChild(container)
    root = createRoot(container)
    await act(async () => {
      root?.render(
        <MemoryRouter initialEntries={["/chat-gui"]}>
          <DashboardAuthIdentityProvider>
            <GuiChatShell />
          </DashboardAuthIdentityProvider>
        </MemoryRouter>,
      )
      await Promise.resolve()
      await Promise.resolve()
    })

    await act(async () => {
      container.querySelector<HTMLButtonElement>("[data-clarify-answer]")?.click()
    })

    expect(connection.respondToClarify).toHaveBeenCalledWith("runtime-a", "clarify-1", "A")
  })

  it("reattaches the committed stored session after a transport close", async () => {
    vi.useFakeTimers();
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    const container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root?.render(
        <MemoryRouter initialEntries={["/chat-gui"]}>
          <DashboardAuthIdentityProvider>
            <GuiChatShell />
          </DashboardAuthIdentityProvider>
        </MemoryRouter>,
      );
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(connection.createOrAttach).toHaveBeenCalledOnce();
    await act(async () => {
      connection.emitState("closed");
      await vi.advanceTimersByTimeAsync(1_200);
    });

    expect(connection.createOrAttach).toHaveBeenCalledTimes(2);
    expect(connection.createOrAttach).toHaveBeenLastCalledWith(
      "stored-a",
      expect.any(Number),
      expect.any(AbortSignal),
      undefined,
    );
    vi.useRealTimers();
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

  it("reuses one connection while navigating between resumed sessions", async () => {
    window.__HERMES_AUTH_REQUIRED__ = false;
    const connection = createConnection();
    mocks.connectGuiChat.mockReturnValue(connection);
    let navigate: ReturnType<typeof useNavigate> | null = null;

    const container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root?.render(
        <MemoryRouter initialEntries={["/chat-gui?resume=session-a"]}>
          <DashboardAuthIdentityProvider>
            <NavigationProbe
              onReady={(nextNavigate) => {
                navigate = nextNavigate;
              }}
            />
            <GuiChatShell />
          </DashboardAuthIdentityProvider>
        </MemoryRouter>,
      );
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(navigate).not.toBeNull();
    for (const sessionId of ["session-b", "session-c", "session-d"]) {
      await act(async () => {
        navigate?.(`/chat-gui?resume=${sessionId}`);
        await Promise.resolve();
        await Promise.resolve();
      });
    }

    expect(mocks.connectGuiChat).toHaveBeenCalledOnce();
    expect(
      connection.createOrAttachMock.mock.calls.map(([sessionId, generation]) => ({
        generation,
        sessionId,
      })),
    ).toEqual([
      { generation: 1, sessionId: "session-a" },
      { generation: 2, sessionId: "session-b" },
      { generation: 3, sessionId: "session-c" },
      { generation: 4, sessionId: "session-d" },
    ]);
    expect(connection.close).not.toHaveBeenCalled();

    await act(async () => root?.unmount());
    root = null;
    expect(connection.close).toHaveBeenCalledOnce();
  });
});

async function renderShell(shell: ReactNode) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(
      <MemoryRouter initialEntries={["/chat-gui"]}>
        <DashboardAuthIdentityProvider>{shell}</DashboardAuthIdentityProvider>
      </MemoryRouter>,
    );
    await Promise.resolve();
    await Promise.resolve();
  });
}

function ReadyProbe() {
  const { ready } = useDashboardAuthIdentity();
  return <span data-ready={ready} />;
}

function NavigationProbe({
  onReady,
}: {
  onReady(navigate: ReturnType<typeof useNavigate>): void;
}) {
  const navigate = useNavigate();
  onReady(navigate);
  return null;
}

type TestGuiChatConnection = GuiChatConnection & {
  createOrAttachMock: ReturnType<typeof vi.fn<GuiChatConnection["createOrAttach"]>>;
  emitState(state: ConnectionState): void;
};

function createConnection(): TestGuiChatConnection {
  const eventHandlers = new Set<(event: never) => void>();
  const stateHandlers = new Set<(state: ConnectionState) => void>();
  const createOrAttachMock = vi.fn<GuiChatConnection["createOrAttach"]>().mockResolvedValue({
    info: { cwd: "/tmp", model: "test-model" },
    session_id: "runtime-a",
    stored_session_id: "stored-a",
  });
  const connection = {
    attachFile: vi.fn(),
    attachImage: vi.fn(),
    attachPdf: vi.fn(),
    client: {
      onEvent: (handler: (event: never) => void) => {
        eventHandlers.add(handler);
        return () => eventHandlers.delete(handler);
      },
      onState: (handler: (state: ConnectionState) => void) => {
        stateHandlers.add(handler);
        handler("idle");
        return () => stateHandlers.delete(handler);
      },
    },
    close: vi.fn(),
    createOrAttach: createOrAttachMock,
    createOrAttachMock,
    emitState: (state: ConnectionState) => {
      for (const handler of stateHandlers) handler(state);
    },
    loadEarlier: vi.fn(),
    ping: vi.fn(),
    reportFrameQueueDiagnostic: vi.fn(),
    respondToApproval: vi.fn().mockResolvedValue(undefined),
    respondToClarify: vi.fn().mockResolvedValue(undefined),
    send: vi.fn().mockResolvedValue(undefined),
    stop: vi.fn(),
  };
  return connection;
}

interface AuthIdentity {
  display_name: string;
  email: string;
  expires_at: number;
  features?: { weixin_ilink_connect?: boolean };
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
