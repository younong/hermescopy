// @vitest-environment jsdom

import "../../__tests__/runtimeMock";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { KanbanApi } from "../../api";
import type { KanbanTaskDetailResponse } from "../../types";
import { KanbanTaskDrawer } from "../KanbanTaskDrawer";


let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = "";
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
});

describe("KanbanTaskDrawer", () => {
  it("shows markdown overview, dependencies, attachments, and home subscriptions", async () => {
    const subscribeHome = vi.fn().mockResolvedValue({ ok: true });
    const api = drawerApi({ subscribeHome });
    await renderDrawer(api);
    await flush();

    expect(document.body.textContent).toContain("Detailed task body");
    expect(document.body.textContent).toContain("parent-1");
    expect(document.body.textContent).toContain("spec.pdf");
    expect(document.body.textContent).toContain("Telegram home");
    const home = document.body.querySelector<HTMLInputElement>('.gui-chat-kanban-home input');
    await act(async () => home?.click());
    await flush();
    expect(subscribeHome).toHaveBeenCalledWith("default", "T-1", "telegram");
  });

  it("adds comments and exposes runs, logs, recovery, and diagnostics", async () => {
    const addComment = vi.fn().mockResolvedValue({ ok: true });
    const getTaskLog = vi.fn().mockResolvedValue({ task_id: "T-1", path: "/tmp/log", exists: true, size_bytes: 3, content: "worker output", truncated: false });
    const reclaimTask = vi.fn().mockResolvedValue({ ok: true });
    const api = drawerApi({ addComment, getTaskLog, reclaimTask });
    await renderDrawer(api);

    await act(async () => buttonNamed(document.body, "Activity")?.click());
    changeValue(document.body.querySelector("textarea"), "Looks good");
    await act(async () => buttonNamed(document.body, "Comment")?.click());
    await flush();
    expect(addComment).toHaveBeenCalledWith("default", "T-1", "Looks good");

    await act(async () => buttonNamed(document.body, "Runs")?.click());
    expect(document.body.textContent).toContain("#7");
    await act(async () => buttonNamed(document.body, "Worker log")?.click());
    await flush();
    expect(document.body.textContent).toContain("worker output");
    expect(document.body.textContent).toContain("coder");
    await act(async () => buttonNamed(document.body, "Reclaim")?.click());
    await flush();
    expect(reclaimTask).toHaveBeenCalledWith("default", "T-1", "Reclaimed from Chat GUI");

    await act(async () => buttonNamed(document.body, "Diagnostics")?.click());
    expect(document.body.textContent).toContain("Worker stalled");
  });
});

async function renderDrawer(api: KanbanApi) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => root?.render(<KanbanTaskDrawer allTasks={[]} api={api} board="default" detail={detail} error={null} loading={false} onClose={vi.fn()} onDelete={vi.fn()} onEdit={vi.fn()} onRefresh={vi.fn().mockResolvedValue(undefined)} />));
}

const detail: KanbanTaskDetailResponse = {
  task: {
    id: "T-1", title: "Native drawer", body: "**Detailed task body**", assignee: "coder", status: "running", priority: 2,
    created_by: "test", created_at: 1, started_at: 2, completed_at: null, workspace_kind: "scratch", workspace_path: null,
    claim_lock: "lock", claim_expires: null, tenant: null, branch_name: null, project_id: null, result: null,
    idempotency_key: null, consecutive_failures: 0, worker_pid: 42, last_failure_error: null, max_runtime_seconds: null,
    last_heartbeat_at: null, current_run_id: 7, workflow_template_id: null, current_step_key: null, workflow: null, skills: null,
    model_override: null, max_retries: null, goal_mode: false, goal_max_turns: null, session_id: null, block_kind: null,
    block_recurrences: 0, age: { created_age_seconds: 1, started_age_seconds: 1, time_to_complete_seconds: null }, latest_summary: null,
    diagnostics: [{ kind: "stalled", severity: "warning", title: "Worker stalled", detail: "No heartbeat", actions: [], first_seen_at: 1, last_seen_at: 2, count: 1, run_id: 7, data: {} }],
  },
  comments: [], events: [], links: { parents: ["parent-1"], children: [] },
  attachments: [{ id: 4, task_id: "T-1", filename: "spec.pdf", content_type: "application/pdf", size: 1200, uploaded_by: "test", stored_path: "/tmp/spec.pdf", created_at: 1 }],
  runs: [{ id: 7, task_id: "T-1", profile: "coder", step_key: null, status: "running", claim_lock: "lock", claim_expires: null, worker_pid: 42, max_runtime_seconds: null, last_heartbeat_at: null, started_at: 2, ended_at: null, outcome: null, summary: null, metadata: null, error: null }],
};

function drawerApi(overrides: Partial<KanbanApi>): KanbanApi {
  return {
    getHomeChannels: vi.fn().mockResolvedValue({ home_channels: [{ platform: "telegram", chat_id: "1", thread_id: "", name: "Telegram home", subscribed: false }] }),
    subscribeHome: vi.fn(), unsubscribeHome: vi.fn(), addComment: vi.fn(), addLink: vi.fn(), deleteLink: vi.fn(),
    uploadAttachment: vi.fn(), downloadAttachment: vi.fn(), deleteAttachment: vi.fn(), specifyTask: vi.fn(), decomposeTask: vi.fn(),
    getTaskLog: vi.fn(), reclaimTask: vi.fn(), reassignTask: vi.fn(), inspectRun: vi.fn(), terminateRun: vi.fn(),
    listProfiles: vi.fn().mockResolvedValue({ profiles: [{ name: "coder" }] }),
    ...overrides,
  } as unknown as KanbanApi;
}

function changeValue(element: Element | null, value: string) {
  if (!(element instanceof HTMLTextAreaElement)) throw new Error("Expected textarea");
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
  act(() => { setter?.call(element, value); element.dispatchEvent(new Event("input", { bubbles: true })); });
}

function buttonNamed(rootNode: ParentNode, text: string) {
  return Array.from(rootNode.querySelectorAll<HTMLButtonElement>("button")).find((button) => button.textContent?.trim() === text);
}

async function flush() { await act(async () => { await Promise.resolve(); await Promise.resolve(); }); }
