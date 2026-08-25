import { describe, expect, it } from "vitest";

import type { GatewayEvent } from "@/lib/gatewayClient";
import { collaborationReducer } from "../reducer";
import type {
  CollaborationApproval,
  CollaborationEvent,
  CollaborationSnapshot,
  CollaborationTarget,
} from "../types";
import { initialCollaborationState } from "../types";

const group = {
  archived_at: null,
  created_at: 1,
  creator_employee_id: null,
  creator_kind: "owner" as const,
  group_id: "group-a",
  last_sequence: 0,
  name: "Research",
  status: "active" as const,
  updated_at: 1,
};

function event(sequence: number, eventId = `event-${sequence}`): CollaborationEvent {
  return {
    actor_employee_id: null,
    actor_kind: "owner",
    actor_membership_id: null,
    body: { text: `message-${sequence}` },
    created_at: sequence,
    event_id: eventId,
    event_kind: "message",
    group_id: group.group_id,
    sequence,
  };
}

function snapshot(
  events: CollaborationEvent[],
  afterSequence = 0,
  overrides: Partial<CollaborationSnapshot> = {},
): CollaborationSnapshot {
  const lastSequence = Math.max(0, ...events.map((item) => item.sequence));
  const direction = afterSequence > 0 ? "forward" : "initial";
  return {
    approvals: [],
    attachments: [],
    events,
    group: { ...group, last_sequence: lastSequence },
    history_page: {
      after_sequence: direction === "forward" ? afterSequence : null,
      before_sequence: null,
      direction,
      has_more: false,
      limit: 100,
      next_after_sequence: direction === "forward" ? lastSequence : null,
      next_before_sequence: direction === "initial" && events.length > 0 ? events[0].sequence : null,
      range_end_sequence: events.at(-1)?.sequence ?? null,
      range_start_sequence: events[0]?.sequence ?? null,
      snapshot_sequence: lastSequence,
      through_sequence: lastSequence,
    },
    memberships: [],
    reconciliation: {
      after_sequence: afterSequence,
      last_sequence: lastSequence,
      next_after_sequence: lastSequence,
      snapshot_authoritative: true,
    },
    targets: [],
    turns: [],
    ...overrides,
  };
}

function gatewayEvent(type: string, payload: Record<string, unknown>): GatewayEvent {
  return { payload, type };
}

function target(targetId: string, executionId: string): CollaborationTarget {
  return {
    employee_id: `account-${targetId}`,
    active_seconds: 0,
    attempt: 1,
    error: null,
    execution_id: executionId,
    membership_id: `membership-${targetId}`,
    result: null,
    snapshot_sequence: 1,
    status: "running",
    target_id: targetId,
    turn_id: "turn-a",
    updated_at: 1,
  };
}

describe("collaborationReducer", () => {
  it("reconciles authoritative entities while merging out-of-order incremental events", () => {
    const initial = collaborationReducer(initialCollaborationState, {
      snapshot: snapshot([event(1), event(2)]),
      type: "snapshot",
    });
    const currentTarget = target("target-current", "execution-current");
    const incremental = snapshot([event(4), event(3), event(2, "duplicate-sequence")], 2, {
      approvals: [],
      attachments: [],
      memberships: [],
      targets: [currentTarget],
      turns: [],
    });

    const reconciled = collaborationReducer(initial, { snapshot: incremental, type: "snapshot" });

    expect(Object.keys(reconciled.eventsBySequence).map(Number)).toEqual([1, 2, 3, 4]);
    expect(reconciled.eventsBySequence[2].event_id).toBe("event-2");
    expect(reconciled.targetsById).toEqual({ "target-current": currentTarget });
    expect(reconciled.lastSequence).toBe(4);
  });

  it("separates backward history cursors from the forward reconciliation watermark", () => {
    const initial = collaborationReducer(initialCollaborationState, {
      snapshot: snapshot([event(101), event(102)], 0, {
        group: { ...group, last_sequence: 200 },
        history_page: {
          after_sequence: null,
          before_sequence: null,
          direction: "initial",
          has_more: true,
          limit: 2,
          next_after_sequence: null,
          next_before_sequence: 101,
          range_end_sequence: 102,
          range_start_sequence: 101,
          snapshot_sequence: 200,
          through_sequence: 200,
        },
      }),
      type: "snapshot",
    });

    expect(initial.reconciledSequence).toBe(200);
    expect(initial.historyBeforeSequence).toBe(101);
    expect(initial.historyHasMore).toBe(true);

    const partialForward = collaborationReducer(initial, {
      snapshot: snapshot([event(201)], 200, {
        group: { ...group, last_sequence: 202 },
        history_page: {
          after_sequence: 200,
          before_sequence: null,
          direction: "forward",
          has_more: true,
          limit: 1,
          next_after_sequence: 201,
          next_before_sequence: null,
          range_end_sequence: 201,
          range_start_sequence: 201,
          snapshot_sequence: 202,
          through_sequence: 202,
        },
      }),
      type: "snapshot",
    });

    expect(partialForward.reconciledSequence).toBe(200);
    expect(partialForward.historyBeforeSequence).toBe(101);
    expect(partialForward.historyHasMore).toBe(true);

    const completedForward = collaborationReducer(partialForward, {
      snapshot: snapshot([event(202)], 201, {
        group: { ...group, last_sequence: 202 },
        history_page: {
          after_sequence: 201,
          before_sequence: null,
          direction: "forward",
          has_more: false,
          limit: 1,
          next_after_sequence: 202,
          next_before_sequence: null,
          range_end_sequence: 202,
          range_start_sequence: 202,
          snapshot_sequence: 202,
          through_sequence: 202,
        },
      }),
      type: "snapshot",
    });

    expect(completedForward.reconciledSequence).toBe(202);
    expect(completedForward.historyBeforeSequence).toBe(101);
    expect(completedForward.historyHasMore).toBe(true);
  });

  it("accepts a legacy reconciliation-only snapshot during a rolling deployment", () => {
    const legacy = snapshot([event(1), event(2)]);
    delete legacy.history_page;

    const loaded = collaborationReducer(initialCollaborationState, {
      snapshot: legacy,
      type: "snapshot",
    });

    expect(Object.keys(loaded.eventsBySequence).map(Number)).toEqual([1, 2]);
    expect(loaded.reconciledSequence).toBe(2);
    expect(loaded.historyHasMore).toBe(false);
  });

  it("preserves a live event that arrives before the initial snapshot", () => {
    const live = collaborationReducer(initialCollaborationState, {
      event: gatewayEvent("collaboration.event.appended", event(3) as unknown as Record<string, unknown>),
      type: "event",
    });
    const loaded = collaborationReducer(live, {
      snapshot: snapshot([event(1), event(2)], 0, {
        group: { ...group, last_sequence: 2 },
        history_page: {
          after_sequence: null,
          before_sequence: null,
          direction: "initial",
          has_more: false,
          limit: 100,
          next_after_sequence: null,
          next_before_sequence: null,
          range_end_sequence: 2,
          range_start_sequence: 1,
          snapshot_sequence: 2,
          through_sequence: 2,
        },
      }),
      type: "snapshot",
    });

    expect(Object.keys(loaded.eventsBySequence).map(Number)).toEqual([1, 2, 3]);
    expect(loaded.lastSequence).toBe(3);
    expect(loaded.reconciledSequence).toBe(2);
  });

  it("fills a low sequence gap after observing a higher live event", () => {
    let state = collaborationReducer(initialCollaborationState, {
      snapshot: snapshot([event(1)], 0, {
        group: { ...group, last_sequence: 3 },
        history_page: {
          after_sequence: null,
          before_sequence: null,
          direction: "initial",
          has_more: false,
          limit: 100,
          next_after_sequence: null,
          next_before_sequence: null,
          range_end_sequence: 1,
          range_start_sequence: 1,
          snapshot_sequence: 1,
          through_sequence: 1,
        },
      }),
      type: "snapshot",
    });
    state = collaborationReducer(state, {
      event: gatewayEvent("collaboration.event.appended", event(3) as unknown as Record<string, unknown>),
      type: "event",
    });
    state = collaborationReducer(state, {
      snapshot: snapshot([event(2), event(3)], 1, {
        group: { ...group, last_sequence: 3 },
        history_page: {
          after_sequence: 1,
          before_sequence: null,
          direction: "forward",
          has_more: false,
          limit: 100,
          next_after_sequence: 3,
          next_before_sequence: null,
          range_end_sequence: 3,
          range_start_sequence: 2,
          snapshot_sequence: 3,
          through_sequence: 3,
        },
      }),
      type: "snapshot",
    });

    expect(Object.keys(state.eventsBySequence).map(Number)).toEqual([1, 2, 3]);
    expect(state.lastSequence).toBe(3);
    expect(state.reconciledSequence).toBe(3);
  });

  it("rejects duplicate, invalid, and cross-group appended events idempotently", () => {
    const loaded = collaborationReducer(initialCollaborationState, {
      snapshot: snapshot([event(1)]),
      type: "snapshot",
    });
    const duplicate = collaborationReducer(loaded, {
      event: gatewayEvent("collaboration.event.appended", event(2, "event-1") as unknown as Record<string, unknown>),
      type: "event",
    });
    const occupiedSequence = collaborationReducer(duplicate, {
      event: gatewayEvent("collaboration.event.appended", event(1, "new-id") as unknown as Record<string, unknown>),
      type: "event",
    });
    const oldMissingSequence = collaborationReducer(occupiedSequence, {
      event: gatewayEvent("collaboration.event.appended", event(0, "old-missing") as unknown as Record<string, unknown>),
      type: "event",
    });
    const crossGroup = { ...event(2), group_id: "group-b" };
    const unchanged = collaborationReducer(oldMissingSequence, {
      event: gatewayEvent("collaboration.event.appended", crossGroup as unknown as Record<string, unknown>),
      type: "event",
    });

    expect(duplicate).toBe(loaded);
    expect(occupiedSequence).toBe(loaded);
    expect(oldMissingSequence).toBe(loaded);
    expect(unchanged).toBe(loaded);
  });

  it("keeps simultaneous execution streams independent and clears only the terminal target", () => {
    const targetA = target("target-a", "execution-a");
    const targetB = target("target-b", "execution-b");
    let state = collaborationReducer(initialCollaborationState, {
      snapshot: snapshot([], 0, { targets: [targetA, targetB] }),
      type: "snapshot",
    });
    for (const [executionId, targetId, text] of [
      ["execution-a", "target-a", "A1"],
      ["execution-b", "target-b", "B1"],
      ["execution-a", "target-a", "A2"],
    ]) {
      state = collaborationReducer(state, {
        event: gatewayEvent("collaboration.execution.delta", {
          execution_id: executionId,
          group_id: group.group_id,
          target_id: targetId,
          text,
          turn_id: "turn-a",
        }),
        type: "event",
      });
    }

    expect(state.executionsById).toEqual({ "execution-a": "A1A2", "execution-b": "B1" });

    state = collaborationReducer(state, {
      event: gatewayEvent("collaboration.target.changed", {
        ...targetA,
        group_id: group.group_id,
        status: "completed",
        updated_at: 2,
      }),
      type: "event",
    });
    expect(state.executionsById).toEqual({ "execution-b": "B1" });
    expect(state.targetsById["target-a"].status).toBe("completed");
  });

  it("rejects stale target and approval regressions while accepting terminal reconciliation", () => {
    const running = target("target-a", "execution-a");
    const pending: CollaborationApproval = {
      approval_id: "approval-a",
      execution_id: running.execution_id,
      group_id: group.group_id,
      status: "pending",
      target_id: running.target_id,
      turn_id: running.turn_id,
      updated_at: 2,
    };
    let state = collaborationReducer(initialCollaborationState, {
      snapshot: snapshot([], 0, { approvals: [pending], targets: [running] }),
      type: "snapshot",
    });
    state = collaborationReducer(state, {
      event: gatewayEvent("collaboration.target.changed", {
        ...running,
        group_id: group.group_id,
        status: "completed",
        updated_at: 3,
      }),
      type: "event",
    });
    state = collaborationReducer(state, {
      event: gatewayEvent("collaboration.target.changed", {
        ...running,
        group_id: group.group_id,
        status: "running",
        updated_at: 2,
      }),
      type: "event",
    });
    state = collaborationReducer(state, {
      event: gatewayEvent("collaboration.approval.changed", {
        ...pending,
        status: "approved",
        updated_at: 3,
      }),
      type: "event",
    });
    state = collaborationReducer(state, {
      event: gatewayEvent("collaboration.approval.changed", pending as unknown as Record<string, unknown>),
      type: "event",
    });

    expect(state.targetsById[running.target_id].status).toBe("completed");
    expect(state.approvalsById[pending.approval_id].status).toBe("approved");
  });

  it("keeps durable target and approval state while discarding ephemeral streams on disconnect", () => {
    const waiting = { ...target("target-a", "execution-a"), status: "waiting_approval" as const };
    const approval: CollaborationApproval = {
      approval_id: "approval-a",
      execution_id: waiting.execution_id,
      group_id: group.group_id,
      request: { allow_permanent: false, summary: "Read project files" },
      status: "pending",
      target_id: waiting.target_id,
      turn_id: waiting.turn_id,
    };
    let state = collaborationReducer(initialCollaborationState, {
      snapshot: snapshot([], 0, { approvals: [approval], targets: [waiting] }),
      type: "snapshot",
    });
    state = collaborationReducer(state, {
      event: gatewayEvent("collaboration.execution.delta", {
        execution_id: waiting.execution_id,
        group_id: group.group_id,
        target_id: waiting.target_id,
        text: "partial",
        turn_id: waiting.turn_id,
      }),
      type: "event",
    });
    state = collaborationReducer(state, { state: "closed", type: "connection" });

    expect(state.executionsById).toEqual({});
    expect(state.targetsById[waiting.target_id].status).toBe("waiting_approval");
    expect(state.approvalsById[approval.approval_id].request).toEqual({
      allow_permanent: false,
      summary: "Read project files",
    });
  });
});
