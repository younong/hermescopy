// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { initialCollaborationState, type CollaborationState } from "../types";
import { GroupConversation } from "./GroupConversation";

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
});

function renderConversation(state: CollaborationState) {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(
      <GroupConversation
        employees={[{ available: true, employeeId: "employee-a", name: "Alice" }]}
        onApproval={vi.fn()}
        onStop={vi.fn()}
        state={state}
      />,
    );
  });
  return container;
}
