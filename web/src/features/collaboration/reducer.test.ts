import { describe, expect, it } from "vitest";

import type { GatewayEvent } from "@/lib/gatewayClient";
import { collaborationReducer } from "./reducer";
import type {
  CollaborationApproval,
  CollaborationEvent,
  CollaborationSnapshot,
  CollaborationTarget,
} from "./types";
import { initialCollaborationState } from "./types";

const group = {
  archived_at: null,
  created_at: 1,
  creator_account_id: null,
  creator_kind: "owner" as const,
  group_id: "group-a",
  last_sequence: 0,
  name: "Research",
  status: "active" as const,
  updated_at: 1,
};

function event(sequence: number, eventId = `event-${sequence}`): CollaborationEvent {
  return {
    actor_account_id: null,
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
  return {
    approvals: [],
    attachments: [],
    events,
    group: { ...group, last_sequence: lastSequence },
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
    account_id: `account-${targetId}`,
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

  it("rejects duplicate, old, and cross-group appended events idempotently", () => {
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
