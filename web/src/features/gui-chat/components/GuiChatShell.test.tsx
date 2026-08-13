// @vitest-environment jsdom

import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
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
  connectMockGuiChat: vi.fn(),
  createILinkEnrollment: vi.fn(),
  getAuthMe: vi.fn(),
  getILinkEnrollment: vi.fn(),
  getSessionMessages: vi.fn(),
  logout: vi.fn(),
  navigationStartedAt: vi.fn(),
  startGuiChatLatencyTrace: vi.fn(),
  getModelRegistrations: vi.fn(),
  activateModelRegistration: vi.fn(),
  getSessions: vi.fn(),
  getEmptySessionsCount: vi.fn(),
  getSessionStats: vi.fn(),
  getMessagingPlatforms: vi.fn(),
  getEmployees: vi.fn(),
  getEmployeeCatalog: vi.fn(),
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
      getSessions: mocks.getSessions,
      getEmptySessionsCount: mocks.getEmptySessionsCount,
      getSessionStats: mocks.getSessionStats,
      getMessagingPlatforms: mocks.getMessagingPlatforms,
      getEmployees: mocks.getEmployees,
      getEmployeeCatalog: mocks.getEmployeeCatalog,
      logout: mocks.logout,
    },
  };
});

vi.mock("../api", () => ({
  connectGuiChat: mocks.connectGuiChat,
}));

vi.mock("../mock", () => ({
  connectMockGuiChat: mocks.connectMockGuiChat,
}));

vi.mock("../latencyTrace", () => ({
  navigationStartedAt: mocks.navigationStartedAt,
  startGuiChatLatencyTrace: mocks.startGuiChatLatencyTrace,
}));

vi.mock("@/contexts/usePageHeader", () => ({
  usePageHeader: () => ({ setAfterTitle: vi.fn(), setEnd: vi.fn() }),
}));

vi.mock("@/contexts/useSystemActions", () => ({
  useSystemActions: () => ({ activeAction: null, actionStatus: null, dismissLog: vi.fn() }),
}));

vi.mock("@/i18n", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/i18n")>();
  const { en } = await import("@/i18n/en");
  return {
    ...actual,
    useI18n: () => ({ locale: "zh", setLocale: vi.fn(), t: en }),
  };
});

vi.mock("@/components/ChatSessionList", () => ({
  ChatSessionList: (props: { refreshNonce?: number }) => (
    <span data-session-refresh-nonce={props.refreshNonce ?? 0} />
  ),
}));

vi.mock("./Composer", () => ({
  Composer: (props: Record<string, unknown>) => (
    <div>
      <span data-composer-reused-file>
        {(props.attachmentToQueue as { file?: File } | undefined)?.file?.name}
      </span>
      <button
        data-composer-ack-reused-file
        onClick={() => {
          const request = props.attachmentToQueue as { requestId?: number } | undefined;
          if (request?.requestId !== undefined) {
            (props.onAttachmentQueued as (requestId: number) => void)?.(request.requestId);
          }
        }}
      >
        Ack reused attachment
      </button>
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
    </div>
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
        data-use-attachment-again
        onClick={() =>
          void (props.onUseAttachmentAgain as (...args: unknown[]) => unknown)({
            downloadUrl: "/api/files/download?path=%2Fworkspace%2Fnotes.txt",
            id: "stored-file",
            kind: "file",
            mimeType: "text/plain",
            name: "notes.txt",
            refText: "@file:/workspace/notes.txt",
            sizeBytes: 5,
            sourcePath: "/workspace/notes.txt",
          })
        }
      >
        Use attachment again
      </button>
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

vi.mock("./GuiChatModelsPane", () => ({
  GuiChatModelsPane: () => <section data-models-pane>Models pane</section>,
}));

vi.mock("./GuiChatScheduledTasksPane", () => ({
  GuiChatScheduledTasksPane: () => <section data-scheduled-tasks-pane>Scheduled tasks pane</section>,
}));

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  mocks.connectGuiChat.mockReset();
  mocks.connectMockGuiChat.mockReset();
  mocks.createILinkEnrollment.mockReset();
  mocks.getAuthMe.mockReset();
  mocks.getILinkEnrollment.mockReset();
  mocks.getSessionMessages.mockReset();
  mocks.getSessions.mockReset();
  mocks.getSessions.mockResolvedValue({ sessions: [], total: 0, limit: 20, offset: 0 });
  mocks.getEmptySessionsCount.mockReset();
  mocks.getEmptySessionsCount.mockResolvedValue({ count: 0 });
  mocks.getSessionStats.mockReset();
  mocks.getSessionStats.mockResolvedValue({
    total: 0,
    active_store: 0,
    archived: 0,
    messages: 0,
    by_source: {},
  });
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
      voice: { model: "", provider: "", registration_id: null },
      vector: { model: "", provider: "", registration_id: null },
    },
    registrations: [
      {
        credential_configured: null,
        id: "chat-a",
        kind: "chat",
        mutable: true,
        scope: "user",
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
        mutable: true,
        scope: "user",
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
        mutable: true,
        scope: "user",
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
  mocks.getMessagingPlatforms.mockReset();
  mocks.getEmployees.mockReset();
  mocks.getEmployees.mockResolvedValue({ employees: [] });
  mocks.getEmployeeCatalog.mockReset();
  mocks.getEmployeeCatalog.mockResolvedValue({
    knowledge_roots: [],
    mcp_servers: [],
    model_registrations: [],
    skills: [],
    toolsets: [],
    workspace: { default: "default", root: "" },
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
  it("downloads a stored attachment before queuing an explicit reuse", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("notes", {
        headers: { "Content-Type": "text/plain" },
        status: 200,
      }),
    );

    await renderShell(<GuiChatShell />);
    await act(async () => {
      document.querySelector<HTMLButtonElement>("[data-use-attachment-again]")?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/files/download?path=%2Fworkspace%2Fnotes.txt",
      expect.objectContaining({ credentials: "include" }),
    );
    expect(document.querySelector("[data-composer-reused-file]")?.textContent).toBe("notes.txt");
  });

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
    expect(sidebar?.querySelector('[aria-label="Manage models"]')?.textContent).toContain("Models");
    expect(sidebar?.querySelector('[aria-label="Employees"]')?.textContent).toContain("Employees");
    expect(sidebar?.querySelector('[aria-label="Message composition statistics"]')?.textContent).toContain("Message statistics");
    const languageSwitcher = sidebar?.querySelector<HTMLButtonElement>('[aria-label="Switch language"]');
    expect(languageSwitcher?.textContent).toContain("简体中文");
    expect(languageSwitcher?.className).toContain("gui-chat-language-trigger");
    expect(document.querySelector('main header [aria-label="Switch language"]')).toBeNull();
    expect(document.querySelector('main header [aria-label="Manage models"]')).toBeNull();
    expect(document.querySelector('[aria-label="Log out"]')).not.toBeNull();
  });

  it("explains how to make an unavailable employee chat available", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);
    const employeeChatButton = document.querySelector<HTMLButtonElement>(
      '[aria-label="Start employee chat"]',
    );

    expect(employeeChatButton?.disabled).toBe(true);
    expect(employeeChatButton?.getAttribute("aria-describedby")).toBe("employee-chat-notice");
    expect(document.querySelector('[role="status"]')?.textContent).toContain(
      "No available AI employees",
    );
    expect(document.querySelector('[role="status"]')?.textContent).toContain("Employee management");
    expect(document.querySelector('[role="status"]')?.className).toContain("text-red-600");
  });

  it("explains when the employee list could not be loaded", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);
    mocks.getEmployees.mockRejectedValue(new Error("Unavailable"));

    await renderShell(<GuiChatShell />);

    expect(
      document.querySelector<HTMLButtonElement>('[aria-label="Start employee chat"]')?.disabled,
    ).toBe(true);
    expect(document.querySelector('[role="status"]')?.textContent).toContain(
      "AI employees could not be loaded",
    );
  });

  it("starts an active employee direct chat even when group participation is disabled", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);
    mocks.getEmployees.mockResolvedValue({
      employees: [
        {
          employee_id: "employee-a",
          avatar_url: null,
          channels: {},
          collaboration_policy: {
            invite_quota: 5,
            may_create_groups: true,
            may_participate: false,
          },
          lifecycle_status: "active",
          profile: {
            knowledge_relative_paths: [],
            max_iterations: 20,
            mcp_servers: [],
            model_registration_id: "chat-a",
            name: "Researcher",
            schema_version: 1,
            skills: [],
            system_prompt: "Server policy",
            toolsets: [],
            workspace_relative_path: "employees/researcher",
          },
          profile_fingerprint: "sha256:pinned",
          profile_revision: 3,
        },
      ],
    });

    await renderShell(<GuiChatShell />);
    await act(async () => {
      document.querySelector<HTMLButtonElement>('[aria-label="Start employee chat"]')?.click();
      await Promise.resolve();
      const employeeButton = Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
        .find((button) => button.textContent?.trim() === "Researcher");
      employeeButton?.click();
      await Promise.resolve();
    });

    expect(connection.createOrAttach).toHaveBeenLastCalledWith(
      null,
      expect.any(Number),
      expect.any(AbortSignal),
      undefined,
      { employeeId: "employee-a" },
    );
  });

  it("opens employee management inside the dedicated workspace", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(
      <>
        <LocationProbe />
        <GuiChatShell />
      </>,
    );
    await act(async () => {
      document.querySelector<HTMLButtonElement>('[aria-label="Employees"]')?.click();
      await Promise.resolve();
    });

    expect(document.querySelector("[data-location]")?.textContent).toBe("/chat/robots");
    const robotsPane = document.querySelector("[data-robots-pane]");
    expect(robotsPane).not.toBeNull();
    expect(robotsPane?.getAttribute("data-theme")).toBe("chat-workspace");
    expect(document.body.textContent).toContain("Employees");
    expect(
      robotsPane?.querySelector("button.gui-chat-workspace-primary-button")
        ?.textContent,
    ).toBe("Add employee");
    expect(robotsPane?.querySelector("[data-employee-management-pane]")).not.toBeNull();
    expect(document.querySelector("[data-composer-send]")).toBeNull();
    expect(
      document.querySelector<HTMLButtonElement>('[aria-label="Employees"]')
        ?.getAttribute("aria-current"),
    ).toBe("page");
    expect(mocks.getMessagingPlatforms).not.toHaveBeenCalled();
    expect(mocks.getEmployeeCatalog).toHaveBeenCalled();
  });

  it("opens message statistics inside the dedicated workspace", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);
    await act(async () => {
      document.querySelector<HTMLButtonElement>('[aria-label="Message composition statistics"]')?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    const statisticsPane = document.querySelector("[data-statistics-pane]");
    expect(statisticsPane).not.toBeNull();
    expect(statisticsPane?.getAttribute("data-theme")).toBe("chat-workspace");
    expect(statisticsPane?.classList.contains("gui-chat-statistics-pane")).toBe(true);
    expect(document.body.textContent).toContain("Message statistics");
    expect(document.querySelector("[data-composer-send]")).toBeNull();
    expect(Array.from(document.querySelectorAll<HTMLButtonElement>('button[aria-current="page"]'))
      .some((button) => button.textContent?.includes("Message statistics"))).toBe(true);
  });

  it("opens models inside the dedicated workspace instead of a picker dialog", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);
    await act(async () => {
      document.querySelector<HTMLButtonElement>('button[aria-label="Manage models"]')?.click();
      await Promise.resolve();
    });

    expect(document.querySelector("[data-models-pane]")).not.toBeNull();
    expect(document.querySelector('main header [aria-label="Switch language"]')).toBeNull();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(document.querySelector("[data-composer-send]")).toBeNull();
    expect(Array.from(document.querySelectorAll<HTMLButtonElement>('button[aria-current="page"]'))
      .some((button) => button.textContent?.includes("Models"))).toBe(true);
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

    expect(document.body.textContent).toContain("next-model · open");
    expect(document.body.textContent).not.toContain("next-provider");
  });

  it("hides Retry while a real chat connection is healthy", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);
    expect(document.querySelector('[aria-label="Retry"]')).not.toBeNull();

    await act(async () => {
      connection.emitState("open");
      await Promise.resolve();
    });

    expect(document.querySelector('[aria-label="Retry"]')).toBeNull();
    expect(document.querySelector('[aria-label="Refresh"]')).not.toBeNull();
  });

  it("refreshes Recent chats without reconnecting", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);
    expect(document.querySelector("[data-session-refresh-nonce]")?.getAttribute("data-session-refresh-nonce"))
      .toBe("0");
    expect(connection.createOrAttach).toHaveBeenCalledTimes(1);

    await act(async () => {
      document.querySelector<HTMLButtonElement>('[aria-label="Refresh"]')?.click();
      await Promise.resolve();
    });

    expect(document.querySelector("[data-session-refresh-nonce]")?.getAttribute("data-session-refresh-nonce"))
      .toBe("1");
    expect(connection.createOrAttach).toHaveBeenCalledTimes(1);
    expect(connection.ping).not.toHaveBeenCalled();
  });

  it("keeps Replay available in mock mode", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectMockGuiChat.mockReturnValue(connection);

    await renderShellAt("/chat?mock=1");
    await act(async () => {
      connection.emitState("open");
      await Promise.resolve();
    });

    const replay = document.querySelector<HTMLButtonElement>('[aria-label="Replay"]');
    expect(replay).not.toBeNull();
    await act(async () => {
      replay?.click();
      await Promise.resolve();
    });
    expect(connection.createOrAttach).toHaveBeenCalledTimes(2);
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

  it("opens scheduled tasks without reconnecting the chat", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);
    await act(async () => {
      Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
        .find((button) => button.textContent?.includes("Scheduled Tasks"))
        ?.click();
      await Promise.resolve();
    });

    expect(document.querySelector("[data-scheduled-tasks-pane]")).not.toBeNull();
    expect(document.querySelector("[data-composer-send]")).toBeNull();
    expect(Array.from(document.querySelectorAll<HTMLButtonElement>('button[aria-current="page"]'))
      .some((button) => button.textContent?.includes("Scheduled Tasks"))).toBe(true);
    expect(connection.createOrAttach).toHaveBeenCalledTimes(1);
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
      undefined,
    );
  });

  it("opens an archived group from a workspace pane", async () => {
    const connection = createConnection();
    vi.mocked(connection.collaboration.listGroups).mockResolvedValue({
      groups: [{
        archived_at: 1_700_000_100,
        created_at: 1_700_000_000,
        creator_employee_id: null,
        creator_kind: "owner",
        group_id: "group-archived",
        last_sequence: 2,
        name: "Archived project",
        status: "archived",
        updated_at: 1_700_000_100,
      }],
    });
    vi.mocked(connection.collaboration.getGroup).mockImplementation(() => new Promise(() => undefined));
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShellAt(
      "/chat/files",
      <>
        <LocationProbe />
        <GuiChatShell />
      </>,
    );
    await act(async () => {
      const archivedButton = Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
        .find((button) => button.textContent?.includes("Archived (1)"));
      archivedButton?.click();
      await Promise.resolve();
    });
    await act(async () => {
      const groupButton = Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
        .find((button) => button.textContent?.trim() === "Archived project");
      groupButton?.click();
      await Promise.resolve();
    });

    expect(document.querySelector("[data-location]")?.textContent)
      .toBe("/chat?group=group-archived");
    expect(document.querySelector("[data-files-pane]")).toBeNull();
    expect(connection.collaboration.getGroup).toHaveBeenCalledWith(
      "group-archived",
      undefined,
      expect.any(AbortSignal),
    );
  });

  it("opens a group route on the shared gateway without creating a direct session", async () => {
    const connection = createConnection();
    vi.mocked(connection.collaboration.getGroup).mockImplementation(() => new Promise(() => undefined));
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShellAt("/chat?group=group-a");

    expect(new Set(mocks.connectGuiChat.mock.results.map((result) => result.value))).toEqual(new Set([connection]));
    expect(connection.attachOwner).toHaveBeenCalledOnce();
    expect(connection.attachOwner).toHaveBeenCalledWith();
    expect(connection.createOrAttach).not.toHaveBeenCalled();
    expect(connection.collaboration.listGroups).toHaveBeenCalledOnce();
  });

  it("reattaches the group owner scope after a transport close", async () => {
    vi.useFakeTimers();
    const connection = createConnection();
    vi.mocked(connection.collaboration.getGroup).mockImplementation(() => new Promise(() => undefined));
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShellAt("/chat?group=group-a");
    await act(async () => {
      connection.emitState("closed");
      await vi.advanceTimersByTimeAsync(1_200);
    });

    expect(connection.attachOwner).toHaveBeenCalledTimes(2);
    expect(connection.createOrAttach).not.toHaveBeenCalled();
  });

  it("leaves a group conversation for a workspace pane without an effect cleanup error", async () => {
    const connection = createConnection();
    vi.mocked(connection.collaboration.getGroup).mockResolvedValue({
      approvals: [],
      attachments: [],
      events: [],
      group: {
        archived_at: null,
        created_at: 1_700_000_000,
        creator_employee_id: null,
        creator_kind: "owner",
        group_id: "group-a",
        last_sequence: 0,
        name: "Group A",
        status: "active",
        updated_at: 1_700_000_000,
      },
      memberships: [],
      reconciliation: {
        after_sequence: 0,
        last_sequence: 0,
        next_after_sequence: 0,
        snapshot_authoritative: true,
      },
      targets: [],
      turns: [],
    });
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShellAt("/chat?group=group-a");
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(() => {
      document.querySelector<HTMLButtonElement>("button[aria-label='Message composition statistics']")?.click();
    }).not.toThrow();
    await act(async () => {
      await Promise.resolve();
    });

    expect(document.querySelector("[data-statistics-pane]")).not.toBeNull();
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
        <MemoryRouter initialEntries={["/chat"]}>
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
      [{ ownerKey: undefined }],
      [{ ownerKey: "owner-a" }],
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
        <MemoryRouter initialEntries={["/chat"]}>
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
        <MemoryRouter initialEntries={["/chat"]}>
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
        <MemoryRouter initialEntries={["/chat?resume=requested"]}>
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
        <MemoryRouter initialEntries={["/chat?resume=requested"]}>
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
        <MemoryRouter initialEntries={["/chat?resume=session-a"]}>
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
        navigate?.(`/chat?resume=${sessionId}`);
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
  await renderShellAt("/chat", shell);
}

async function renderShellAt(entry: string, shell: ReactNode = <GuiChatShell />) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(
      <MemoryRouter initialEntries={[entry]}>
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

function LocationProbe() {
  const location = useLocation();
  return <span data-location>{`${location.pathname}${location.search}`}</span>;
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
  const collaborationEventHandlers = new Set<(event: never) => void>();
  const connection = {
    attachFile: vi.fn(),
    attachImage: vi.fn(),
    attachOwner: vi.fn().mockResolvedValue(undefined),
    attachPdf: vi.fn(),
    collaboration: {
      archiveGroup: vi.fn(),
      createGroup: vi.fn(),
      getGroup: vi.fn(),
      interruptTarget: vi.fn(),
      listGroups: vi.fn().mockResolvedValue({ groups: [] }),
      onEvent: vi.fn((handler: (event: never) => void) => {
        collaborationEventHandlers.add(handler);
        return () => collaborationEventHandlers.delete(handler);
      }),
      respondToApproval: vi.fn(),
      submitMessage: vi.fn(),
      updateMembers: vi.fn(),
      uploadAttachment: vi.fn(),
    },
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
    ensureConnected: vi.fn().mockResolvedValue(undefined),
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
    setReasoningLevel: vi.fn().mockResolvedValue({ value: "max" }),
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

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}
