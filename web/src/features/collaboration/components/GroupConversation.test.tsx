// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { initialCollaborationState, type CollaborationState } from "../types";
import { GroupConversation } from "./GroupConversation";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/api")>(),
  withHermesAssetAuth: (url: string) => `/hermes${url}`,
}));

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  Element.prototype.scrollIntoView = vi.fn();
  document.body.innerHTML = "";
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
});

describe("GroupConversation owner mentions", () => {
  it("renders structured mentions alongside the clean message text", () => {
    const container = renderConversation({
      ...initialCollaborationState,
      loading: false,
      membershipsById: {
        "membership-a": {
          employee_id: "employee-a",
          created_at: 1,
          group_id: "group-a",
          join_sequence: 1,
          leave_sequence: null,
          left_at: null,
          membership_id: "membership-a",
          profile_fingerprint: "fingerprint-a",
          profile_revision: 1,
          role: "Researcher",
        },
      },
      eventsBySequence: {
        1: {
          actor_employee_id: null,
          actor_kind: "owner",
          actor_membership_id: null,
          body: { mentions: ["membership-a"], text: "Review this" },
          created_at: 1,
          event_id: "event-a",
          event_kind: "message.owner",
          group_id: "group-a",
          sequence: 1,
        },
      },
    });

    expect(container.textContent).toContain("@Alice");
    expect(container.textContent).toContain("Review this");
  });

  it("renders @all from structured event metadata", () => {
    const container = renderConversation({
      ...initialCollaborationState,
      loading: false,
      eventsBySequence: {
        1: {
          actor_employee_id: null,
          actor_kind: "owner",
          actor_membership_id: null,
          body: { mention_all: true, mentions: [], text: "Review this" },
          created_at: 1,
          event_id: "event-a",
          event_kind: "message.owner",
          group_id: "group-a",
          sequence: 1,
        },
      },
    });

    expect(container.textContent).toContain("@all");
  });

  it("renders employee avatars within the dashboard base path", () => {
    const container = renderConversation({
      ...initialCollaborationState,
      loading: false,
      eventsBySequence: {
        1: {
          actor_employee_id: "employee-a",
          actor_kind: "employee",
          actor_membership_id: "membership-a",
          body: { text: "Finished" },
          created_at: 1,
          event_id: "event-a",
          event_kind: "message.employee",
          group_id: "group-a",
          sequence: 1,
        },
      },
    }, "/api/employees/employee-a/avatar?v=123");

    expect(container.querySelector("img")?.getAttribute("src"))
      .toBe("/hermes/api/employees/employee-a/avatar?v=123");
  });

  it("hides completed target status below the user message", () => {
    const container = renderConversation({
      ...initialCollaborationState,
      loading: false,
      eventsBySequence: {
        1: {
          actor_employee_id: null,
          actor_kind: "owner",
          actor_membership_id: null,
          body: { text: "Review this" },
          created_at: 1,
          event_id: "event-a",
          event_kind: "message.owner",
          group_id: "group-a",
          sequence: 1,
        },
      },
      targetsById: {
        "target-a": {
          active_seconds: 1,
          attempt: 1,
          employee_id: "employee-a",
          error: null,
          execution_id: "execution-a",
          membership_id: "membership-a",
          result: { text: "Finished" },
          snapshot_sequence: 1,
          status: "completed",
          target_id: "target-a",
          turn_id: "turn-a",
        },
      },
      turnsById: {
        "turn-a": {
          event_id: "event-a",
          group_id: "group-a",
          snapshot_sequence: 1,
          status: "completed",
          turn_id: "turn-a",
        },
      },
    });

    expect(container.textContent).toContain("Finished");
    expect(container.textContent).not.toContain("已完成");
    expect(container.querySelector(".lucide-circle-check-big")).toBeNull();
  });
});

function renderConversation(state: CollaborationState, avatarUrl?: string) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <GroupConversation
        employees={[{ avatarUrl, available: true, employeeId: "employee-a", name: "Alice" }]}
        onApproval={vi.fn()}
        onStop={vi.fn()}
        state={state}
      />,
    );
  });
  return container;
}
