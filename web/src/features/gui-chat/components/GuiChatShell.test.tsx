// @vitest-environment jsdom

import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
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
  getAuthMe: vi.fn(),
  setEnd: vi.fn(),
  setTitle: vi.fn(),
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
  usePageHeader: () => ({ setEnd: mocks.setEnd, setTitle: mocks.setTitle }),
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
      data-terminal-hint={String(props.showTerminalChatHint)}
      onClick={() =>
        (props.onClarifyRespond as (...args: unknown[]) => unknown)("clarify-1", "A")
      }
    >
      Clarify answer
    </button>
  ),
}));

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  mocks.connectGuiChat.mockReset();
  mocks.getAuthMe.mockReset();
  mocks.setEnd.mockReset();
  mocks.setTitle.mockReset();
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
  it("keeps terminal chat in the default dashboard header", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);

    expect(latestHeaderMarkup()).toContain("Terminal Chat");
    expect(document.querySelector("[data-terminal-hint]")?.getAttribute("data-terminal-hint")).toBe(
      "true",
    );
  });

  it("hides terminal chat and includes custom actions in standalone mode", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(
      <GuiChatShell
        headerActions={<button type="button">Log out</button>}
        showTerminalChatAction={false}
      />,
    );

    const header = latestHeaderMarkup();
    expect(header).not.toContain("Terminal Chat");
    expect(header).toContain("Log out");
    expect(document.querySelector("[data-terminal-hint]")?.getAttribute("data-terminal-hint")).toBe(
      "false",
    );
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

function latestHeaderMarkup(): string {
  const calls = mocks.setEnd.mock.calls.filter(([value]) => value !== null);
  return renderToStaticMarkup(calls.at(-1)?.[0] ?? null);
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
