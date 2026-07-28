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
  getSessionMessages: vi.fn(),
  logout: vi.fn(),
  navigationStartedAt: vi.fn(),
  startGuiChatLatencyTrace: vi.fn(),
  getModelRegistrations: vi.fn(),
  activateModelRegistration: vi.fn(),
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
      getSessionMessages: mocks.getSessionMessages,
      getModelRegistrations: mocks.getModelRegistrations,
      activateModelRegistration: mocks.activateModelRegistration,
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

vi.mock("../latencyTrace", () => ({
  navigationStartedAt: mocks.navigationStartedAt,
  startGuiChatLatencyTrace: mocks.startGuiChatLatencyTrace,
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
    <div>
      <div data-message-text>
        {(props.state as { messages?: Array<{ text: string }> }).messages
          ?.map((message) => message.text)
          .join("|")}
      </div>
      <button
        data-clarify-answer
        onClick={() =>
          (props.onClarifyRespond as (...args: unknown[]) => unknown)("clarify-1", "A")
        }
      >
        Clarify answer
      </button>
    </div>
  ),
}));

vi.mock("@/features/files/components/GuiChatFilesPane", () => ({
  GuiChatFilesPane: () => <section data-files-pane>Files pane</section>,
}));

vi.mock("./GuiChatSkillsPane", () => ({
  GuiChatSkillsPane: () => <section data-skills-pane>Skills pane</section>,
}));

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  mocks.connectGuiChat.mockReset();
  mocks.createILinkEnrollment.mockReset();
  mocks.getAuthMe.mockReset();
  mocks.getILinkEnrollment.mockReset();
  mocks.getSessionMessages.mockReset();
  mocks.getSessionMessages.mockResolvedValue({
    history_page: { cursor: null, has_more: false, returned_count: 0 },
    messages: [],
    session_id: "stored-a",
  });
  mocks.logout.mockReset();
  mocks.navigationStartedAt.mockReset();
  mocks.navigationStartedAt.mockReturnValue(undefined);
  mocks.startGuiChatLatencyTrace.mockReset();
  mocks.getModelRegistrations.mockReset();
  mocks.getModelRegistrations.mockResolvedValue({
    active: {
      chat: { model: "test-model", provider: "test-provider", registration_id: "chat-a" },
      image: { model: "image-old", provider: "image-provider", registration_id: null },
      video: { model: "", provider: "", registration_id: null },
    },
    registrations: [
      {
        credential_configured: null,
        id: "chat-a",
        kind: "chat",
        model: "test-model",
        name: "Test model",
        provider: "test-provider",
        source: "catalog",
        use_gateway: false,
      },
      {
        credential_configured: null,
        id: "chat-b",
        kind: "chat",
        model: "next-model",
        name: "Next model",
        provider: "next-provider",
        source: "catalog",
        use_gateway: false,
      },
      {
        credential_configured: null,
        id: "image-a",
        kind: "image",
        model: "image-new",
        name: "Image model",
        provider: "image-provider",
        source: "catalog",
        use_gateway: false,
      },
    ],
  });
  mocks.activateModelRegistration.mockReset();
  mocks.activateModelRegistration.mockResolvedValue({
    kind: "image",
    model: "image-new",
    ok: true,
    provider: "image-provider",
    registration_id: "image-a",
  });
  mocks.startGuiChatLatencyTrace.mockImplementation(() => ({
    id: "trace-initial-123",
    mark: vi.fn(),
  }));
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
    const sidebar = document.querySelector('aside[aria-label="Chat workspace"]');
    expect(sidebar).not.toBeNull();
    expect(sidebar?.querySelector('[aria-label="Switch registered model"]')?.textContent).toContain("Models");
    expect(document.querySelector('main header [aria-label="Switch registered model"]')).toBeNull();
    expect(document.querySelector('[aria-label="Log out"]')).not.toBeNull();
  });

  it("switches the conversation through the registered chat model picker", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);
    await act(async () => {
      connection.emitState("open");
      await Promise.resolve();
    });
    await openModelPicker();

    expect(document.body.textContent).toContain("Switch registered model");
    await act(async () => {
      buttonWithText("Next model")?.click();
    });
    await act(async () => {
      buttonWithText("Switch", true)?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(connection.switchModel).toHaveBeenCalledWith(
      "runtime-a",
      "next-provider",
      "next-model",
      false,
      false,
    );
  });

  it("can make a registered chat model the default for new conversations", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);
    await act(async () => {
      connection.emitState("open");
      await Promise.resolve();
    });
    await openModelPicker();
    await act(async () => {
      buttonWithText("Next model")?.click();
      const checkbox = document.querySelector<HTMLInputElement>(
        'input[type="checkbox"]',
      );
      checkbox?.click();
    });
    await act(async () => {
      buttonWithText("Switch", true)?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(connection.switchModel).toHaveBeenCalledWith(
      "runtime-a",
      "next-provider",
      "next-model",
      false,
      true,
    );
  });

  it("confirms an expensive chat model before retrying the session switch", async () => {
    const connection = createConnection();
    connection.switchModel
      .mockResolvedValueOnce({
        confirm_message: "This model can cost significantly more.",
        confirm_required: true,
        value: "next-model",
      })
      .mockResolvedValueOnce({ confirm_required: false, value: "next-model" });
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);
    await act(async () => {
      connection.emitState("open");
      await Promise.resolve();
    });
    await openModelPicker();
    await act(async () => {
      buttonWithText("Next model")?.click();
    });
    await act(async () => {
      buttonWithText("Switch", true)?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(document.body.textContent).toContain("This model can cost significantly more.");
    expect(connection.switchModel).toHaveBeenCalledWith(
      "runtime-a",
      "next-provider",
      "next-model",
      false,
      false,
    );

    await act(async () => {
      buttonWithText("Switch anyway", true)?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(connection.switchModel).toHaveBeenLastCalledWith(
      "runtime-a",
      "next-provider",
      "next-model",
      true,
      false,
    );
    expect(document.body.textContent).not.toContain("Switch registered model");
  });

  it("blocks all registered-model switching while generation is busy", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);
    await act(async () => {
      connection.emitState("open");
      await Promise.resolve();
    });
    await act(async () => {
      connection.emitEvent({
        payload: {},
        session_id: "runtime-a",
        type: "message.start",
      });
    });
    await openModelPicker();
    await act(async () => {
      buttonWithText("Next model")?.click();
    });

    expect(buttonWithText("Switch", true)?.disabled).toBe(true);
    expect(document.querySelector('[role="alert"]')?.textContent).toContain(
      "Stop the current response before switching models.",
    );

    await act(async () => {
      buttonWithText("Image", true)?.click();
    });
    expect(buttonWithText("Activate", true)?.disabled).toBe(true);

    expect(mocks.activateModelRegistration).not.toHaveBeenCalled();
    expect(connection.switchModel).not.toHaveBeenCalled();
  });

  it("updates the displayed provider from session info after switching", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);
    await act(async () => {
      connection.emitState("open");
      await Promise.resolve();
    });
    await act(async () => {
      connection.emitEvent({
        payload: { model: "next-model", provider: "next-provider" },
        session_id: "runtime-a",
        type: "session.info",
      });
    });

    expect(document.body.textContent).toContain("next-model · next-provider · open");
  });

  it("activates registered image models through REST", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);
    await act(async () => {
      connection.emitState("open");
      await Promise.resolve();
    });
    await openModelPicker();
    await act(async () => {
      buttonWithText("Image", true)?.click();
    });
    await act(async () => {
      buttonWithText("Activate", true)?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mocks.activateModelRegistration).toHaveBeenCalledWith("image-a", "");
    expect(connection.switchModel).not.toHaveBeenCalled();
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

  it("opens skills inside the dedicated workspace and returns to chat", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);
    await act(async () => {
      Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
        .find((button) => button.textContent?.includes("Skills"))
        ?.click();
      await Promise.resolve();
    });

    expect(document.querySelector('aside[aria-label="Chat workspace"]')).not.toBeNull();
    expect(document.querySelector("[data-skills-pane]")).not.toBeNull();
    expect(document.querySelector("[data-composer-send]")).toBeNull();
    expect(Array.from(document.querySelectorAll<HTMLButtonElement>('button[aria-current="page"]'))
      .some((button) => button.textContent?.includes("Skills"))).toBe(true);
    expect(connection.createOrAttach).toHaveBeenCalledTimes(1);

    await act(async () => {
      Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
        .find((button) => button.textContent?.includes("New chat"))
        ?.click();
      await Promise.resolve();
    });

    expect(document.querySelector("[data-skills-pane]")).toBeNull();
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

  it("shows a safe explanation without enrolling when the enabled connector is unavailable", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue({
      ...authIdentity(),
      features: { weixin_ilink_connect: false },
      feature_status: {
        weixin_ilink_connect: {
          enabled: true,
          ready: false,
          state: "resource_governance_unavailable",
          message: "WeChat connection is not available on this server yet.",
        },
      },
    });
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);

    const connect = document.querySelector<HTMLButtonElement>('[aria-label="Connect WeChat"]');
    expect(connect).not.toBeNull();
    await act(async () => {
      connect?.click();
      await Promise.resolve();
    });
    expect(document.querySelector('[role="dialog"]')?.textContent).toContain(
      "WeChat connection is not available on this server yet.",
    );
    expect(mocks.createILinkEnrollment).not.toHaveBeenCalled();
  });

  it("hides the WeChat action when the connector is explicitly disabled", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue({
      ...authIdentity(),
      features: { weixin_ilink_connect: false },
      feature_status: {
        weixin_ilink_connect: {
          enabled: false,
          ready: false,
          state: "disabled",
          message: "WeChat connection is disabled on this server.",
        },
      },
    });
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

  it("traces the initial physical connection from navigation", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);
    mocks.navigationStartedAt.mockReturnValue(0);

    await renderShell(<GuiChatShell />);

    expect(mocks.startGuiChatLatencyTrace).toHaveBeenCalledOnce();
    expect(mocks.startGuiChatLatencyTrace).toHaveBeenCalledWith(
      "connection.start",
      { startedAt: 0 },
    );
    expect(connection.createOrAttach).toHaveBeenCalledWith(
      null,
      expect.any(Number),
      expect.any(AbortSignal),
      expect.objectContaining({ traceId: "trace-initial-123" }),
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
    expect(document.querySelector("[data-gui-chat]")).not.toBeNull();
    expect(document.querySelector('aside[aria-label="Chat workspace"]')).not.toBeNull();
    expect(document.body.textContent).toContain("idle");
    expect(connection.createOrAttach).toHaveBeenCalledOnce();
    expect(connection.createOrAttach).toHaveBeenCalledWith(
      null,
      expect.any(Number),
      expect.any(AbortSignal),
      expect.objectContaining({ traceId: "trace-initial-123" }),
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

  it("loads durable history without waiting for runtime attach", async () => {
    const connection = createConnection();
    const attach = deferred<Awaited<ReturnType<GuiChatConnection["createOrAttach"]>>>();
    const history = deferred<{
      history_page: { cursor: null; has_more: false; returned_count: number };
      messages: Array<{ id: string; role: "assistant"; text: string }>;
      session_id: string;
    }>();
    connection.createOrAttachMock.mockReturnValue(attach.promise);
    mocks.getSessionMessages.mockReturnValue(history.promise);
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

    await act(async () => {
      history.resolve({
        history_page: { cursor: null, has_more: false, returned_count: 1 },
        messages: [{ id: "db-requested-1", role: "assistant", text: "saved answer" }],
        session_id: "requested",
      });
      await history.promise;
    });

    expect(mocks.getSessionMessages).toHaveBeenCalledWith(
      "requested",
      expect.objectContaining({ limit: 100, signal: expect.any(AbortSignal) }),
      "",
    );
    expect(connection.createOrAttach).toHaveBeenCalledOnce();
    expect(container.textContent).toContain("saved answer");

    await act(async () => {
      attach.resolve({ session_id: "runtime-requested" });
      await attach.promise;
      await Promise.resolve();
    });

    expect(container.textContent).toContain("saved answer");
    expect(mocks.getSessionMessages.mock.calls[0]?.[1]?.signal.aborted).toBe(false);
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
  emitEvent(event: Parameters<Parameters<GuiChatConnection["client"]["onEvent"]>[0]>[0]): void;
  emitState(state: ConnectionState): void;
  switchModel: ReturnType<typeof vi.fn<GuiChatConnection["switchModel"]>>;
};

function createConnection(): TestGuiChatConnection {
  const eventHandlers = new Set<(event: never) => void>();
  const stateHandlers = new Set<(state: ConnectionState) => void>();
  const createOrAttachMock = vi.fn<GuiChatConnection["createOrAttach"]>().mockResolvedValue({
    info: { cwd: "/tmp", model: "test-model", provider: "test-provider" },
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
    emitEvent: (event: Parameters<Parameters<GuiChatConnection["client"]["onEvent"]>[0]>[0]) => {
      for (const handler of eventHandlers) handler(event as never);
    },
    emitState: (state: ConnectionState) => {
      for (const handler of stateHandlers) handler(state);
    },
    ping: vi.fn(),
    reportFrameQueueDiagnostic: vi.fn(),
    respondToApproval: vi.fn().mockResolvedValue(undefined),
    respondToClarify: vi.fn().mockResolvedValue(undefined),
    send: vi.fn().mockResolvedValue(undefined),
    stop: vi.fn(),
    switchModel: vi.fn().mockResolvedValue({ confirm_required: false, value: "next-model" }),
  };
  return connection;
}

interface AuthIdentity {
  display_name: string;
  email: string;
  expires_at: number;
  features?: { weixin_ilink_connect?: boolean };
  feature_status?: {
    weixin_ilink_connect?: {
      enabled: boolean;
      ready: boolean;
      state: string;
      message: string;
    };
  };
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

async function openModelPicker() {
  await act(async () => {
    document.querySelector<HTMLButtonElement>('button[aria-label="Switch registered model"]')?.click();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function buttonWithText(text: string, exact = false): HTMLButtonElement | undefined {
  return Array.from(document.querySelectorAll<HTMLButtonElement>("button")).find((button) =>
    exact ? button.textContent?.trim() === text : button.textContent?.includes(text),
  );
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
