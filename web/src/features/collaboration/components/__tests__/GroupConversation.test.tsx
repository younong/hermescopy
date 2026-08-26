// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { initialCollaborationState, type CollaborationState } from "../../types";
import { GroupConversation } from "../GroupConversation";

vi.mock("@/lib/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/api")>(),
  withHermesAssetAuth: (url: string) => `/hermes${url}`,
}));

let root: Root | null = null;
let renderContainer: HTMLDivElement | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  Element.prototype.scrollIntoView = vi.fn();
  document.body.innerHTML = "";
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  renderContainer = null;
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

  it("renders persisted discussion round metadata in the conversation stream", () => {
    const container = renderConversation({
      ...initialCollaborationState,
      loading: false,
      eventsBySequence: {
        1: {
          actor_employee_id: null,
          actor_kind: "owner",
          actor_membership_id: null,
          body: {
            discussion_id: "discussion-a",
            discussion_round: 1,
            mention_all: true,
            mentions: [],
            text: "Discuss this for 3 rounds",
            total_rounds: 3,
          },
          created_at: 1,
          event_id: "event-a",
          event_kind: "message.owner",
          group_id: "group-a",
          sequence: 1,
        },
        2: {
          actor_employee_id: null,
          actor_kind: "system",
          actor_membership_id: null,
          body: {
            discussion_id: "discussion-a",
            discussion_round: 2,
            text: "Discussion round 2 of 3",
            total_rounds: 3,
          },
          created_at: 2,
          event_id: "event-b",
          event_kind: "discussion.round.started",
          group_id: "group-a",
          sequence: 2,
        },
      },
    });

    expect(container.textContent).toContain("Round 1 of 3");
    expect(container.textContent).toContain("Round 2 of 3");
    expect(container.textContent).not.toContain("Discussion round 2 of 3");
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

  it("loads a short initial history once per advancing sequence", () => {
    const onLoadEarlier = vi.fn();
    const baseState: CollaborationState = {
      ...initialCollaborationState,
      group: {
        archived_at: null,
        created_at: 1,
        creator_employee_id: null,
        creator_kind: "owner",
        group_id: "group-a",
        last_sequence: 1,
        name: "Group A",
        status: "active",
        updated_at: 1,
      },
      historyBeforeSequence: 10,
      historyHasMore: true,
      loading: false,
    };

    const container = renderConversation(baseState, undefined, onLoadEarlier);
    expect(onLoadEarlier).toHaveBeenCalledTimes(1);

    renderConversation(baseState, undefined, onLoadEarlier);
    expect(onLoadEarlier).toHaveBeenCalledTimes(1);

    renderConversation({ ...baseState, historyLoading: true }, undefined, onLoadEarlier);
    renderConversation({ ...baseState, historyBeforeSequence: 5 }, undefined, onLoadEarlier);
    expect(onLoadEarlier).toHaveBeenCalledTimes(2);

    renderConversation({
      ...baseState,
      historyBeforeSequence: undefined,
      historyHasMore: false,
    }, undefined, onLoadEarlier);
    expect(onLoadEarlier).toHaveBeenCalledTimes(2);
    expect(container.textContent).not.toContain("Scroll up for earlier messages");
  });

  it("renders the history control when a page has no visible message events", () => {
    const container = renderConversation({
      ...initialCollaborationState,
      eventsBySequence: {
        1: {
          actor_employee_id: null,
          actor_kind: "system",
          actor_membership_id: null,
          body: { membership_id: "membership-a" },
          created_at: 1,
          event_id: "event-a",
          event_kind: "membership.joined",
          group_id: "group-a",
          sequence: 1,
        },
      },
      historyBeforeSequence: 1,
      historyHasMore: true,
      loading: false,
    });

    expect(container.textContent).toContain("Scroll up for earlier messages");
    expect(container.textContent).not.toContain("Start the group conversation");
  });

  it("does not render target cards below the user message", () => {
    const container = renderConversation({
      ...initialCollaborationState,
      approvalsById: {
        "approval-a": {
          approval_id: "approval-a",
          execution_id: "execution-a",
          group_id: "group-a",
          request: { summary: "Allow tool" },
          status: "pending",
          target_id: "target-a",
          turn_id: "turn-a",
        },
      },
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
      executionsById: { "execution-a": "Working draft" },
      loading: false,
      targetsById: {
        "target-a": {
          active_seconds: 1,
          attempt: 1,
          employee_id: "employee-a",
          error: null,
          execution_id: "execution-a",
          membership_id: "membership-a",
          result: null,
          snapshot_sequence: 1,
          status: "waiting_approval",
          target_id: "target-a",
          turn_id: "turn-a",
        },
      },
      turnsById: {
        "turn-a": {
          event_id: "event-a",
          group_id: "group-a",
          snapshot_sequence: 1,
          status: "running",
          turn_id: "turn-a",
        },
      },
    });

    expect(container.textContent).toContain("Review this");
    expect(container.textContent).not.toContain("Alice");
    expect(container.textContent).not.toContain("Working draft");
    expect(container.textContent).not.toContain("Allow tool");
  });
});

function renderConversation(
  state: CollaborationState,
  avatarUrl?: string,
  onLoadEarlier?: () => void | Promise<void>,
) {
  if (!root || !renderContainer) {
    renderContainer = document.createElement("div");
    document.body.appendChild(renderContainer);
    root = createRoot(renderContainer);
  }
  const container = renderContainer;
  act(() => {
    root?.render(
      <GroupConversation
        employees={[{ avatarUrl, available: true, employeeId: "employee-a", name: "Alice" }]}
        onLoadEarlier={onLoadEarlier}
        state={state}
      />,
    );
  });
  return container;
}
