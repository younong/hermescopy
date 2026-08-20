// @vitest-environment jsdom

import "../../__tests__/runtimeMock";
import React, { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { KanbanTask } from "../../types";
import { KanbanBoardView } from "../KanbanBoardView";


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

describe("KanbanBoardView", () => {
  it("supports search, bulk selection, opening, and accessible status changes", async () => {
    const onMove = vi.fn();
    const onOpen = vi.fn();
    const onSelect = vi.fn();
    const container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => root?.render(
      <KanbanBoardView
        assignee=""
        columns={[
          { name: "todo", tasks: [task({ id: "T-1", title: "Ship native board" })] },
          { name: "done", tasks: [task({ id: "T-2", status: "done", title: "Hidden result" })] },
        ]}
        laneByProfile={false}
        mobileStatus="todo"
        onCreate={vi.fn()}
        onMove={onMove}
        onOpen={onOpen}
        onSelect={onSelect}
        query="native"
        selected={new Set()}
      />,
    ));

    expect(container.textContent).toContain("Ship native board");
    expect(container.textContent).not.toContain("Hidden result");
    await act(async () => container.querySelector<HTMLButtonElement>(".gui-chat-kanban-card-title")?.click());
    expect(onOpen).toHaveBeenCalledWith("T-1");

    await act(async () => container.querySelector<HTMLInputElement>('input[type="checkbox"]')?.click());
    expect(onSelect).toHaveBeenCalledWith("T-1", true);

    const select = container.querySelector<HTMLSelectElement>('[aria-label="Status: Ship native board"]');
    setSelect(select, "blocked");
    expect(onMove).toHaveBeenCalledWith(expect.objectContaining({ id: "T-1" }), "blocked");
  });

  it("renders profile lanes and mobile active column state", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => root?.render(
      <KanbanBoardView
        assignee=""
        columns={[
          { name: "todo", tasks: [task({ assignee: "coder", id: "T-1" }), task({ assignee: null, id: "T-2" })] },
          { name: "ready", tasks: [] },
        ]}
        laneByProfile
        mobileStatus="ready"
        onCreate={vi.fn()}
        onMove={vi.fn()}
        onOpen={vi.fn()}
        onSelect={vi.fn()}
        query=""
        selected={new Set(["T-1"])}
      />,
    ));

    expect(container.textContent).toContain("coder");
    expect(container.textContent).toContain("unassigned");
    expect(container.querySelector('[data-status="ready"]')?.classList.contains("is-mobile-active")).toBe(true);
    expect(container.querySelector(".gui-chat-kanban-card.is-selected")).not.toBeNull();
  });
});

function task(overrides: Partial<KanbanTask>): KanbanTask {
  return {
    id: "T-0", title: "Task", body: null, assignee: null, status: "todo", priority: 0,
    created_by: "test", created_at: 1, started_at: null, completed_at: null,
    workspace_kind: "scratch", workspace_path: null, claim_lock: null, claim_expires: null,
    tenant: null, branch_name: null, project_id: null, result: null, idempotency_key: null,
    consecutive_failures: 0, worker_pid: null, last_failure_error: null, max_runtime_seconds: null,
    last_heartbeat_at: null, current_run_id: null, workflow_template_id: null, current_step_key: null, workflow: null,
    skills: null, model_override: null, max_retries: null, goal_mode: false, goal_max_turns: null,
    session_id: null, block_kind: null, block_recurrences: 0,
    age: { created_age_seconds: 1, started_age_seconds: null, time_to_complete_seconds: null },
    latest_summary: null,
    ...overrides,
  };
}

function setSelect(element: HTMLSelectElement | null, value: string) {
  if (!element) throw new Error("Expected select");
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
  act(() => {
    setter?.call(element, value);
    element.dispatchEvent(new Event("change", { bubbles: true }));
  });
}
