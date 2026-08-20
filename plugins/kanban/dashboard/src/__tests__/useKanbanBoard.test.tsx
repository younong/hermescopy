// @vitest-environment jsdom

import "./runtimeMock";
import React, { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { KanbanApi } from "../api";
import type { KanbanBoardResponse, KanbanTaskDetailResponse } from "../types";
import { useKanbanBoard, type UseKanbanBoardOptions } from "../useKanbanBoard";

const emptyBoard = (eventId = 0): KanbanBoardResponse => ({
  columns: [],
  tenants: [],
  assignees: [],
  latest_event_id: eventId,
  now: 1,
});

const taskDetail = (id: string): KanbanTaskDetailResponse => ({
  task: {
    id,
    title: "Selected task",
    body: null,
    assignee: null,
    status: "todo",
    priority: 0,
    created_by: null,
    created_at: 1,
    started_at: null,
    completed_at: null,
    workspace_kind: "scratch",
    workspace_path: null,
    claim_lock: null,
    claim_expires: null,
    tenant: null,
    branch_name: null,
    project_id: null,
    result: null,
    idempotency_key: null,
    consecutive_failures: 0,
    worker_pid: null,
    last_failure_error: null,
    max_runtime_seconds: null,
    last_heartbeat_at: null,
    current_run_id: null,
    workflow_template_id: null,
    current_step_key: null,
    workflow: null,
    skills: null,
    model_override: null,
    max_retries: null,
    goal_mode: false,
    goal_max_turns: null,
    session_id: null,
    block_kind: null,
    block_recurrences: 0,
    age: {
      created_age_seconds: 0,
      started_age_seconds: null,
      time_to_complete_seconds: null,
    },
    latest_summary: null,
  },
  comments: [],
  events: [],
  attachments: [],
  links: { parents: [], children: [] },
  runs: [],
});

class FakeWebSocket {
  readonly url: string;
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  close = vi.fn();

  constructor(url: string) {
    this.url = url;
  }

  open() {
    this.onopen?.(new Event("open"));
  }

  message(data: unknown) {
    this.onmessage?.(new MessageEvent("message", { data: JSON.stringify(data) }));
  }

  disconnect() {
    this.onclose?.(new CloseEvent("close"));
  }
}

type HookValue = ReturnType<typeof useKanbanBoard>;
let root: Root | null = null;
let container: HTMLDivElement | null = null;
let current: HookValue | null = null;

function Harness({ options }: { options: UseKanbanBoardOptions }) {
  const value = useKanbanBoard(options);
  useEffect(() => {
    current = value;
  }, [value]);
  return null;
}

function createApi(overrides: Partial<KanbanApi> = {}): KanbanApi {
  return {
    listBoards: vi.fn().mockResolvedValue({
      boards: [{ slug: "default" }, { slug: "second" }],
      current: "default",
    }),
    createBoard: vi.fn(),
    updateBoard: vi.fn(),
    removeBoard: vi.fn(),
    switchBoard: vi.fn(),
    getBoard: vi.fn().mockResolvedValue(emptyBoard()),
    getTask: vi.fn(),
    createTask: vi.fn(),
    updateWorkflow: vi.fn(),
    updateTask: vi.fn(),
    deleteTask: vi.fn(),
    bulkUpdate: vi.fn(),
    addComment: vi.fn(),
    addLink: vi.fn(),
    deleteLink: vi.fn(),
    listAttachments: vi.fn(),
    uploadAttachment: vi.fn(),
    downloadAttachment: vi.fn(),
    deleteAttachment: vi.fn(),
    getStats: vi.fn(),
    getAssignees: vi.fn(),
    dispatch: vi.fn(),
    getDiagnostics: vi.fn(),
    listActiveWorkers: vi.fn(),
    getRun: vi.fn(),
    inspectRun: vi.fn(),
    terminateRun: vi.fn(),
    getTaskLog: vi.fn(),
    reclaimTask: vi.fn(),
    reassignTask: vi.fn(),
    specifyTask: vi.fn(),
    decomposeTask: vi.fn(),
    getHomeChannels: vi.fn(),
    subscribeHome: vi.fn(),
    unsubscribeHome: vi.fn(),
    getConfig: vi.fn(),
    listProfiles: vi.fn(),
    updateProfileDescription: vi.fn(),
    autoDescribeProfile: vi.fn(),
    getOrchestration: vi.fn(),
    updateOrchestration: vi.fn(),
    buildEventsUrl: vi.fn().mockResolvedValue("wss://example.test/events"),
    ...overrides,
  } as KanbanApi;
}

async function render(options: UseKanbanBoardOptions) {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root?.render(<Harness options={options} />);
  });
}

beforeEach(() => {
  current = null;
  localStorage.clear();
});

afterEach(async () => {
  vi.useRealTimers();
  if (root) await act(async () => root?.unmount());
  container?.remove();
  root = null;
  container = null;
});

describe("useKanbanBoard", () => {
  it("protects board state from stale responses when switching boards", async () => {
    let resolveDefault: ((value: KanbanBoardResponse) => void) | undefined;
    const getBoard = vi.fn((board: string) => {
      if (board === "default") {
        return new Promise<KanbanBoardResponse>((resolve) => {
          resolveDefault = resolve;
        });
      }
      return Promise.resolve({ ...emptyBoard(8), tenants: ["second-result"] });
    });
    const api = createApi({ getBoard });

    await render({ api, webSocketFactory: (url) => new FakeWebSocket(url) as unknown as WebSocket });
    await act(async () => current?.setActiveBoard("second"));
    await vi.waitFor(() => expect(current?.board?.tenants).toEqual(["second-result"]));

    await act(async () => resolveDefault?.({ ...emptyBoard(2), tenants: ["stale-default"] }));
    expect(current?.activeBoard).toBe("second");
    expect(current?.board?.tenants).toEqual(["second-result"]);
    expect(localStorage.getItem("hermes.kanban.board")).toBe("second");
  });

  it("coalesces websocket event bursts without refreshing unrelated task detail", async () => {
    vi.useFakeTimers();
    const sockets: FakeWebSocket[] = [];
    const getBoard = vi.fn().mockResolvedValue(emptyBoard(3));
    const getTask = vi.fn().mockResolvedValue(taskDetail("selected"));
    const api = createApi({ getBoard, getTask });

    await render({
      api,
      webSocketFactory: (url) => {
        const socket = new FakeWebSocket(url);
        sockets.push(socket);
        return socket as unknown as WebSocket;
      },
    });
    expect(sockets).toHaveLength(1);
    await act(async () => current?.setSelectedTaskId("selected"));
    await Promise.resolve();
    const boardCallsBefore = getBoard.mock.calls.length;
    const taskCallsBefore = getTask.mock.calls.length;

    await act(async () => {
      sockets[0]?.message({ events: [{ id: 4, task_id: "other" }], cursor: 4 });
      sockets[0]?.message({ events: [{ id: 5, task_id: "other" }], cursor: 5 });
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(getBoard).toHaveBeenCalledTimes(boardCallsBefore + 1);
    expect(getTask).toHaveBeenCalledTimes(taskCallsBefore);

    await act(async () => {
      sockets[0]?.message({ events: [{ id: 6, task_id: "selected" }], cursor: 6 });
      sockets[0]?.message({ events: [{ id: 7, task_id: "selected" }], cursor: 7 });
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(getBoard).toHaveBeenCalledTimes(boardCallsBefore + 2);
    expect(getTask).toHaveBeenCalledTimes(taskCallsBefore + 1);
  });

  it("reconnects with the latest cursor and cleans up on board changes", async () => {
    vi.useFakeTimers();
    const sockets: FakeWebSocket[] = [];
    const buildEventsUrl = vi.fn((board: string, since: number) =>
      Promise.resolve(`wss://example.test/events?board=${board}&since=${since}`),
    );
    const api = createApi({ buildEventsUrl, getBoard: vi.fn().mockResolvedValue(emptyBoard(6)) });

    await render({
      api,
      reconnectDelayMs: 10,
      webSocketFactory: (url) => {
        const socket = new FakeWebSocket(url);
        sockets.push(socket);
        return socket as unknown as WebSocket;
      },
    });
    await act(async () => sockets[0]?.open());
    await act(async () => {
      sockets[0]?.message({ events: [{ id: 9 }], cursor: 9 });
      sockets[0]?.disconnect();
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(buildEventsUrl).toHaveBeenLastCalledWith("default", 9, expect.any(AbortSignal));
    expect(sockets).toHaveLength(2);

    await act(async () => current?.setActiveBoard("second"));
    expect(sockets[1]?.close).toHaveBeenCalledOnce();
    expect(buildEventsUrl).toHaveBeenLastCalledWith("second", 0, expect.any(AbortSignal));
  });
});
