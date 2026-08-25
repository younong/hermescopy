// @vitest-environment jsdom

import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { GuiChatConnection } from "../../api";
import type { ConnectionState } from "@/lib/gatewayClient";
import type { Employee } from "@/lib/api";
import {
  DashboardAuthIdentityProvider,
  useDashboardAuthIdentity,
} from "@/lib/useDashboardAuthIdentity";
import { dashboardAuthTransition } from "@/lib/dashboardAuthTransition";
import { GuiChatShell } from "../GuiChatShell";

const mocks = vi.hoisted(() => ({
  connectGuiChat: vi.fn(),
  pluginsLoading: false,
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
  getPlugins: vi.fn(),
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
      getPlugins: mocks.getPlugins,
      logout: mocks.logout,
    },
  };
});

vi.mock("../../api", () => ({
  connectGuiChat: mocks.connectGuiChat,
}));

vi.mock("../../mock", () => ({
  connectMockGuiChat: mocks.connectMockGuiChat,
}));

vi.mock("../../latencyTrace", () => ({
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
  ChatSessionList: (props: { activeSessionId?: string | null; refreshNonce?: number }) => (
    <span
      data-active-session-id={props.activeSessionId ?? ""}
      data-session-refresh-nonce={props.refreshNonce ?? 0}
    />
  ),
}));

vi.mock("../Composer", () => ({
  Composer: (props: Record<string, unknown>) => (
    <div>
      <span data-composer-reused-file>
        {(props.attachmentToQueue as { file?: File } | undefined)?.file?.name}
      </span>
      <div data-composer-model-picker>{props.modelPicker as ReactNode}</div>
      <div data-composer-reasoning-picker-slot>{props.reasoningPicker as ReactNode}</div>
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

vi.mock("../MessageList", () => ({
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

vi.mock("@/plugins", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/plugins")>();
  return {
    ...actual,
    ChatPluginWorkspace: ({ workspaceId }: { workspaceId: string }) => (
      <section data-kanban-pane={workspaceId === "kanban" || undefined} data-statistics-pane={workspaceId === "statistics" || undefined}>
        {workspaceId === "kanban" ? "Kanban pane" : "Message statistics"}
      </section>
    ),
    usePlugins: () => ({
      loading: mocks.pluginsLoading,
      manifests: [
        {
          name: "message-statistics",
          label: "Message statistics",
          description: "Message composition statistics",
          icon: "PieChart",
          version: "1.0.0",
          chat: { workspaces: [{ id: "statistics", path: "/chat/statistics", label: "Message statistics", description: "Message composition statistics", icon: "PieChart", position: "after:contacts", admin_only: false }] },
          entry: "dist/index.js",
          has_api: false,
          source: "bundled",
        },
        {
          name: "kanban",
          label: "Kanban",
          description: "Board",
          icon: "SquareKanban",
          version: "1.0.0",
          chat: { workspaces: [{ id: "kanban", path: "/chat/kanban", label: "Board", description: "Board", icon: "SquareKanban", position: "after:files", admin_only: true }] },
          entry: "dist/index.js",
          has_api: true,
          source: "bundled",
        },
      ],
      plugins: [],
    }),
  };
});

vi.mock("../GuiChatSkillsPane", () => ({
  GuiChatSkillsPane: () => <section data-skills-pane>Skills pane</section>,
}));

vi.mock("../GuiChatModelsPane", () => ({
  GuiChatModelsPane: () => <section data-models-pane>Models pane</section>,
}));

vi.mock("../GuiChatScheduledTasksPane", () => ({
  GuiChatScheduledTasksPane: () => <section data-scheduled-tasks-pane>Scheduled tasks pane</section>,
}));

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  mocks.connectGuiChat.mockReset();
  mocks.pluginsLoading = false;
  mocks.connectMockGuiChat.mockReset();
  mocks.createILinkEnrollment.mockReset();
  mocks.getAuthMe.mockReset();
  mocks.getILinkEnrollment.mockReset();
  mocks.getSessionMessages.mockReset();
  mocks.getPlugins.mockReset();
  mocks.getPlugins.mockResolvedValue([
    {
      name: "scheduled-tasks",
      label: "Scheduled Tasks",
      description: "",
      icon: "Clock",
      version: "1.0.0",
      tab: { path: "/cron" },
      entry: "dist/index.js",
      css: null,
      has_api: true,
      source: "bundled",
    },
  ]);
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
  it("holds an unresolved plugin deep link while manifests load without creating a chat", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);
    mocks.pluginsLoading = true;

    await renderShellAt("/chat/future-workspace");

    expect(document.querySelector('[role="status"]')?.textContent).toContain("Loading");
    expect(connection.createOrAttach).not.toHaveBeenCalled();
    await act(async () => root?.unmount());
  });

  it("redirects an unknown plugin path only after manifest discovery completes", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShellAt(
      "/chat/missing-workspace",
      <><GuiChatShell /><LocationProbe /></>,
    );

    await vi.waitFor(() => {
      expect(document.querySelector("[data-location]")?.textContent).toBe("/chat");
    });
    expect(connection.createOrAttach).toHaveBeenCalledOnce();
    await act(async () => root?.unmount());
  });

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

  it("renders one Contacts entry in the dedicated workspace navigation", async () => {
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
    const contactsEntries = sidebar?.querySelectorAll('[aria-label="Contacts"]') ?? [];
    expect(contactsEntries).toHaveLength(1);
    expect(contactsEntries[0]?.textContent).toContain("Contacts");
    expect(sidebar?.querySelector('[aria-label="Employees"]')).toBeNull();
    expect(sidebar?.querySelector('[aria-label="Start employee chat"]')).toBeNull();
    expect(sidebar?.querySelector('[aria-label="Message statistics"]')?.textContent).toContain("Message statistics");
    expect(Array.from(sidebar?.querySelectorAll("button") ?? [])
      .some((button) => button.textContent?.includes("Board"))).toBe(true);
    const languageSwitcher = sidebar?.querySelector<HTMLButtonElement>('[aria-label="Switch language"]');
    expect(languageSwitcher?.textContent).toContain("简体中文");
    expect(languageSwitcher?.className).toContain("gui-chat-language-trigger");
    expect(document.querySelector('main header [aria-label="Switch language"]')).toBeNull();
    expect(document.querySelector('main header [aria-label="Manage models"]')).toBeNull();
    expect(document.querySelector('[aria-label="Log out"]')).not.toBeNull();
  });

  it("opens the controlled contacts list without creating an untargeted session", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);
    mocks.getEmployees.mockResolvedValue({ employees: [employee()] });

    await renderShell(
      <>
        <LocationProbe />
        <GuiChatShell />
      </>,
    );
    expect(connection.createOrAttach).toHaveBeenCalledOnce();

    await act(async () => {
      document.querySelector<HTMLButtonElement>('[aria-label="Contacts"]')?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(document.querySelector("[data-location]")?.textContent).toBe("/chat/contacts");
    expect(document.querySelector("[data-employee-contacts-pane]")).not.toBeNull();
    expect(document.querySelector('[role="list"][aria-label="Employee list"]')?.textContent)
      .toContain("Researcher");
    expect(document.body.textContent).toContain("Choose a contact to start a conversation");
    expect(document.querySelector("[data-composer-send]")).toBeNull();
    expect(document.querySelector('[aria-label="Contacts"]')?.getAttribute("aria-current"))
      .toBe("page");
    expect(connection.createOrAttach).toHaveBeenCalledOnce();
  });

  it("routes an eligible built-in contact without a stored profile to its conversation", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);
    mocks.getEmployees.mockResolvedValue({
      employees: [builtinEmployee({
        employee_id: "employee-a",
        nickname: "Nova",
      })],
    });

    await renderShellAt(
      "/chat/contacts",
      <>
        <LocationProbe />
        <GuiChatShell />
      </>,
    );
    expect(connection.createOrAttach).not.toHaveBeenCalled();

    await act(async () => {
      Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
        .find((button) => button.textContent?.includes("Nova") && !button.hasAttribute("aria-label"))
        ?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(document.querySelector("[data-location]")?.textContent).toBe("/chat?employee=employee-a");
    expect(connection.createOrAttach).toHaveBeenCalledOnce();
    expect(connection.createOrAttach).toHaveBeenLastCalledWith(
      null,
      expect.any(Number),
      expect.any(AbortSignal),
      undefined,
      { employeeId: "employee-a" },
    );
    expect(document.querySelector("[data-composer-send]")).not.toBeNull();
  });

  it("hides the model and reasoning pickers in employee conversations", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);
    mocks.getEmployees.mockResolvedValue({ employees: [employee()] });

    await renderShellAt("/chat?employee=employee-a");

    expect(document.querySelector("[data-composer-send]")).not.toBeNull();
    expect(document.querySelector("[data-composer-model-picker]")?.childElementCount).toBe(0);
    expect(document.querySelector("[data-composer-reasoning-picker-slot]")?.childElementCount).toBe(0);
  });

  it("reopens employee routes across A to B to A navigation and reload", async () => {
    window.__HERMES_AUTH_REQUIRED__ = false;
    const employees = [employee(), employee({ employee_id: "employee-b", name: "Writer" })];
    mocks.getEmployees.mockResolvedValue({ employees });
    const connection = createConnection();
    connection.createOrAttachMock.mockImplementation(async (_target, _generation, _signal, _timing, options) => ({
      info: { cwd: "/tmp", model: "test-model", provider: "test-provider" },
      messages: options?.employeeId === "employee-a"
        ? [{ id: "employee-a-message", role: "user", text: "employee A transcript" }]
        : [],
      session_id: `runtime-${options?.employeeId ?? "new"}`,
      stored_session_id: `stored-${options?.employeeId ?? "new"}`,
    }));
    mocks.connectGuiChat.mockReturnValue(connection);
    let navigate: ReturnType<typeof useNavigate> | null = null;

    await renderShellAt(
      "/chat?employee=employee-a",
      <>
        <NavigationProbe onReady={(nextNavigate) => { navigate = nextNavigate; }} />
        <GuiChatShell />
      </>,
    );

    for (const nextEmployeeId of ["employee-b", "employee-a"]) {
      await act(async () => {
        navigate?.(`/chat?employee=${nextEmployeeId}`);
        await Promise.resolve();
        await Promise.resolve();
      });
    }

    expect(connection.createOrAttachMock.mock.calls.map((call) => call[4])).toEqual([
      { employeeId: "employee-a" },
      { employeeId: "employee-b" },
      { employeeId: "employee-a" },
    ]);
    expect(connection.createOrAttachMock.mock.calls.map((call) => call[1])).toEqual([1, 2, 3]);
    expect(mocks.connectGuiChat).toHaveBeenCalledOnce();
    expect(document.querySelector("[data-message-text]")?.textContent)
      .toContain("employee A transcript");

    await act(async () => root?.unmount());
    root = null;
    const reloadConnection = createConnection();
    reloadConnection.createOrAttachMock.mockResolvedValue({
      info: { cwd: "/tmp", model: "test-model", provider: "test-provider" },
      messages: [{ id: "employee-a-message", role: "user", text: "employee A transcript" }],
      session_id: "runtime-employee-a",
      stored_session_id: "stored-employee-a",
    });
    mocks.connectGuiChat.mockReturnValue(reloadConnection);
    await renderShellAt("/chat?employee=employee-a");

    expect(reloadConnection.createOrAttach).toHaveBeenCalledOnce();
    expect(reloadConnection.createOrAttach).toHaveBeenCalledWith(
      null,
      expect.any(Number),
      expect.any(AbortSignal),
      undefined,
      { employeeId: "employee-a" },
    );
    expect(document.querySelector("[data-message-text]")?.textContent)
      .toContain("employee A transcript");
  });

  it("uses the committed stored session for recent-chat highlighting on employee routes", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);
    mocks.getEmployees.mockResolvedValue({ employees: [employee()] });

    await renderShellAt("/chat?employee=employee-a");

    expect(document.querySelector("[data-active-session-id]")?.getAttribute("data-active-session-id"))
      .toBe("stored-a");
  });

  it("clears recent-chat highlighting when an employee route switches to a group", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);
    mocks.getEmployees.mockResolvedValue({ employees: [employee()] });
    vi.mocked(connection.collaboration.getGroup).mockResolvedValue({
      approvals: [],
      attachments: [],
      events: [],
      group: {
        archived_at: null,
        created_at: 1,
        creator_employee_id: null,
        creator_kind: "owner",
        group_id: "group-a",
        last_sequence: 0,
        name: "Research",
        status: "active",
        updated_at: 1,
      },
      history_page: {
        after_sequence: null,
        before_sequence: null,
        direction: "initial",
        has_more: false,
        limit: 100,
        next_after_sequence: null,
        next_before_sequence: null,
        range_end_sequence: null,
        range_start_sequence: null,
        snapshot_sequence: 0,
        through_sequence: 0,
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
    let navigate: ReturnType<typeof useNavigate> | null = null;

    await renderShellAt(
      "/chat?employee=employee-a",
      <>
        <NavigationProbe onReady={(nextNavigate) => { navigate = nextNavigate; }} />
        <GuiChatShell />
      </>,
    );
    expect(document.querySelector("[data-active-session-id]")?.getAttribute("data-active-session-id"))
      .toBe("stored-a");

    await act(async () => {
      navigate?.("/chat?group=group-a");
      await Promise.resolve();
    });

    expect(document.querySelector("[data-active-session-id]")?.getAttribute("data-active-session-id"))
      .toBe("");
  });

  it("redirects unavailable employee routes without starting a session", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);
    mocks.getEmployees.mockResolvedValue({
      employees: [employee({ lifecycle_status: "suspended" })],
    });

    await renderShellAt(
      "/chat?employee=employee-a",
      <>
        <LocationProbe />
        <GuiChatShell />
      </>,
    );

    expect(document.querySelector("[data-location]")?.textContent).toBe("/chat/contacts");
    expect(connection.createOrAttach).not.toHaveBeenCalled();
  });

  it("redirects legacy contact URLs to the stable employee route", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);
    mocks.getEmployees.mockResolvedValue({ employees: [employee()] });

    await renderShellAt(
      "/chat/contacts/employee-a",
      <>
        <LocationProbe />
        <GuiChatShell />
      </>,
    );

    expect(document.querySelector("[data-location]")?.textContent).toBe("/chat?employee=employee-a");
    expect(connection.createOrAttach).toHaveBeenCalledOnce();
    expect(connection.createOrAttach).toHaveBeenCalledWith(
      null,
      expect.any(Number),
      expect.any(AbortSignal),
      undefined,
      { employeeId: "employee-a" },
    );
  });

  it("does not start unavailable contacts or select a contact from its settings button", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);
    mocks.getEmployees.mockResolvedValue({
      employees: [employee(), employee({ employee_id: "employee-b", lifecycle_status: "suspended", name: "Writer" })],
    });

    await renderShellAt(
      "/chat/contacts",
      <>
        <LocationProbe />
        <GuiChatShell />
      </>,
    );

    const unavailable = Array.from(document.querySelectorAll<HTMLButtonElement>("button"))
      .find((button) => button.textContent?.includes("Writer") && !button.hasAttribute("aria-label"));
    expect(unavailable?.getAttribute("aria-disabled")).toBe("true");
    await act(async () => unavailable?.click());
    expect(document.querySelector("[data-location]")?.textContent).toBe("/chat/contacts");
    expect(connection.createOrAttach).not.toHaveBeenCalled();

    await act(async () => {
      document.querySelector<HTMLButtonElement>('[aria-label="Manage employee: Researcher"]')?.click();
      await Promise.resolve();
    });
    expect(document.querySelector("[data-location]")?.textContent).toBe("/chat/contacts");
    expect(connection.createOrAttach).not.toHaveBeenCalled();
    expect(Array.from(document.querySelectorAll('[role="dialog"]'))
      .find((dialog) => dialog.textContent?.includes("Manage the employee profile")))
      .not.toBeUndefined();
  });

  it("shows Back to contacts for a selected contact on mobile", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);
    mocks.getEmployees.mockResolvedValue({ employees: [employee()] });
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      addEventListener: vi.fn(),
      matches: query === "(max-width: 1023px)",
      media: query,
      removeEventListener: vi.fn(),
    }));

    await renderShellAt(
      "/chat?employee=employee-a",
      <>
        <LocationProbe />
        <GuiChatShell />
      </>,
    );

    const back = document.querySelector<HTMLButtonElement>('[aria-label="Back to contacts"]');
    expect(back).not.toBeNull();
    expect(document.querySelector("[data-employee-contacts-pane]")).toBeNull();
    await act(async () => {
      back?.click();
      await Promise.resolve();
    });
    expect(document.querySelector("[data-location]")?.textContent).toBe("/chat/contacts");
  });

  it("opens message statistics inside the dedicated workspace", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);
    await act(async () => {
      document.querySelector<HTMLButtonElement>('[aria-label="Message statistics"]')?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    const statisticsPane = document.querySelector("[data-statistics-pane]");
    expect(statisticsPane).not.toBeNull();
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

  it("submits only the final local model selection and keeps it selected", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);
    await act(async () => {
      connection.emitState("open");
      await Promise.resolve();
    });

    const trigger = document.querySelector<HTMLButtonElement>(
      'button[aria-label="Switch chat model"]',
    );
    await act(async () => {
      trigger?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    const nextOption = Array.from(
      document.querySelectorAll<HTMLButtonElement>('[role="option"]'),
    ).find((button) => button.textContent?.includes("next-model"));
    await act(async () => nextOption?.click());
    expect(connection.preflightModel).not.toHaveBeenCalled();
    expect(connection.send).not.toHaveBeenCalled();

    await act(async () => {
      document.querySelector<HTMLButtonElement>("[data-composer-send]")?.click();
      await Promise.resolve();
      await Promise.resolve();
    });

    const registration = {
      id: "chat-b",
      model: "next-model",
      provider: "next-provider",
    };
    expect(connection.preflightModel).toHaveBeenCalledWith("runtime-a", registration);
    expect(connection.send).toHaveBeenCalledWith(
      "runtime-a",
      "new message",
      { confirmExpensiveModel: false, modelRegistration: registration },
    );
    expect(document.querySelector('[data-message-text]')?.textContent).toContain("new message");
    expect(document.querySelector('[aria-label="Switch chat model"]')?.textContent)
      .toContain("next-model");
  });

  it("cancels an expensive-model send without appending a user message", async () => {
    const connection = createConnection();
    vi.mocked(connection.preflightModel).mockResolvedValue({
      confirm_message: "High price",
      confirm_required: true,
      value: "next-model",
    });
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);
    await act(async () => {
      connection.emitState("open");
      await Promise.resolve();
    });
    await act(async () => {
      document.querySelector<HTMLButtonElement>('button[aria-label="Switch chat model"]')?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    await act(async () => {
      Array.from(document.querySelectorAll<HTMLButtonElement>('[role="option"]'))
        .find((button) => button.textContent?.includes("next-model"))
        ?.click();
      await Promise.resolve();
    });
    await act(async () => {
      document.querySelector<HTMLButtonElement>("[data-composer-send]")?.click();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(document.body.textContent).toContain("High price");

    await act(async () => {
      Array.from(document.querySelectorAll<HTMLButtonElement>('button'))
        .find((button) => button.textContent?.trim() === "Cancel")
        ?.click();
      await Promise.resolve();
    });

    expect(connection.send).not.toHaveBeenCalled();
    expect(document.querySelector('[data-message-text]')?.textContent).not.toContain("new message");
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

  it("opens the Kanban board from the workspace navigation", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(
      <>
        <LocationProbe />
        <GuiChatShell />
      </>,
    );
    const sidebar = document.querySelector('aside[aria-label="Chat workspace"]');
    const kanbanButton = Array.from(sidebar?.querySelectorAll<HTMLButtonElement>("button") ?? [])
      .find((button) => button.textContent?.trim() === "Board");

    expect(kanbanButton?.querySelector(".lucide-square-kanban")).not.toBeNull();
    await act(async () => {
      kanbanButton?.click();
      await Promise.resolve();
    });

    expect(document.querySelector("[data-location]")?.textContent).toBe("/chat/kanban");
    expect(document.querySelector("[data-kanban-pane]")).not.toBeNull();
    expect(document.querySelector("[data-composer-send]")).toBeNull();
    expect(kanbanButton?.getAttribute("aria-current")).toBe("page");
    expect(document.querySelector("main header h1")?.textContent).toBe("Board");
    expect(connection.createOrAttach).toHaveBeenCalledTimes(1);
  });

  it("opens a direct Kanban workspace route", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShellAt("/chat/kanban");

    expect(document.querySelector("[data-kanban-pane]")).not.toBeNull();
    expect(document.querySelector("main header h1")?.textContent).toBe("Board");
    expect(Array.from(document.querySelectorAll<HTMLButtonElement>('button[aria-current="page"]'))
      .some((button) => button.textContent?.trim() === "Board")).toBe(true);
  });

  it("opens Kanban from the mobile drawer and closes the drawer", async () => {
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      addEventListener: vi.fn(),
      matches: query === "(max-width: 1023px)",
      media: query,
      removeEventListener: vi.fn(),
    }));
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(
      <>
        <LocationProbe />
        <GuiChatShell />
      </>,
    );
    const drawerButton = document.querySelector<HTMLButtonElement>('[aria-label="Open sessions"]');
    await act(async () => {
      drawerButton?.click();
      await Promise.resolve();
    });
    expect(drawerButton?.getAttribute("aria-expanded")).toBe("true");

    const mobileSidebar = document.querySelector("aside.gui-chat-mobile-sidebar");
    const kanbanButton = Array.from(mobileSidebar?.querySelectorAll<HTMLButtonElement>("button") ?? [])
      .find((button) => button.textContent?.trim() === "Board");
    await act(async () => {
      kanbanButton?.click();
      await Promise.resolve();
    });

    expect(document.querySelector("[data-location]")?.textContent).toBe("/chat/kanban");
    expect(document.querySelector("[data-kanban-pane]")).not.toBeNull();
    expect(drawerButton?.getAttribute("aria-expanded")).toBe("false");
    expect(mobileSidebar?.className).toContain("-translate-x-full");
  });

  it("hides scheduled tasks when the plugin is disabled", async () => {
    const connection = createConnection();
    mocks.getAuthMe.mockResolvedValue(authIdentity());
    mocks.getPlugins.mockResolvedValue([]);
    mocks.connectGuiChat.mockReturnValue(connection);

    await renderShell(<GuiChatShell />);

    expect(
      Array.from(document.querySelectorAll<HTMLButtonElement>("button")).some(
        (button) => button.textContent?.includes("Scheduled Tasks"),
      ),
    ).toBe(false);
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
      { limit: 100 },
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
      history_page: {
        after_sequence: null,
        before_sequence: null,
        direction: "initial",
        has_more: false,
        limit: 100,
        next_after_sequence: null,
        next_before_sequence: null,
        range_end_sequence: null,
        range_start_sequence: null,
        snapshot_sequence: 0,
        through_sequence: 0,
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
      document.querySelector<HTMLButtonElement>("button[aria-label='Message statistics']")?.click();
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
  setDefaultModel: ReturnType<typeof vi.fn<GuiChatConnection["setDefaultModel"]>>;
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
    preflightModel: vi.fn().mockResolvedValue({ confirm_required: false }),
    send: vi.fn().mockResolvedValue(undefined),
    setDefaultModel: vi.fn().mockResolvedValue({ confirm_required: false, value: "next-model" }),
    setReasoningLevel: vi.fn().mockResolvedValue({ value: "max" }),
    stop: vi.fn(),
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

function employee(overrides: {
  employee_id?: string;
  lifecycle_status?: Employee["lifecycle_status"];
  name?: string;
  protected?: boolean;
} = {}): Extract<Employee, { employee_kind: "managed" }> {
  const employeeId = overrides.employee_id ?? "employee-a";
  const lifecycleStatus = overrides.lifecycle_status ?? "active";
  return {
    avatar_url: null,
    channels: {},
    chat_eligible: lifecycleStatus === "active",
    employee_kind: "managed",
    protected: overrides.protected ?? false,
    collaboration_policy: {
      invite_quota: 5,
      may_create_groups: true,
      may_create_scheduled_tasks: true,
      may_participate: false,
    },
    employee_id: employeeId,
    lifecycle_status: lifecycleStatus,
    profile: {
      knowledge_relative_paths: [],
      max_iterations: 20,
      max_tokens: null,
      mcp_servers: [],
      model_registration_id: "chat-a",
      name: overrides.name ?? "Researcher",
      role: "Analyst",
      schema_version: 1,
      skills: [],
      system_prompt: "Server policy",
      toolsets: [],
      workspace_relative_path: `employees/${employeeId}`,
    },
    profile_fingerprint: "sha256:pinned",
    profile_revision: 3,
  };
}

function builtinEmployee(overrides: Partial<Extract<Employee, { employee_kind: "builtin_assistant" }>> & {
  nickname?: string;
} = {}): Extract<Employee, { employee_kind: "builtin_assistant" }> {
  const { nickname = "AI Assistant", ...employeeOverrides } = overrides;
  return {
    avatar_url: null,
    builtin_assistant_personalization: { nickname, personal_preference: "" },
    channels: {},
    chat_eligible: true,
    collaboration_policy: {
      invite_quota: null,
      may_create_groups: true,
      may_create_scheduled_tasks: true,
      may_participate: true,
    },
    employee_id: "employee-a",
    employee_kind: "builtin_assistant",
    lifecycle_status: "active",
    profile: null,
    profile_fingerprint: "sha256:builtin",
    profile_revision: 3,
    protected: true,
    ...employeeOverrides,
  };
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
