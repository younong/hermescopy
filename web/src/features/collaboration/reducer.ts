import type { GatewayEvent } from "@/lib/gatewayClient";
import { collaborationGroupId, isCollaborationEvent } from "./protocol";
import type {
  CollaborationApproval,
  CollaborationEvent,
  CollaborationSnapshot,
  CollaborationState,
  CollaborationTarget,
} from "./types";
import { initialCollaborationState } from "./types";

export type CollaborationAction =
  | { type: "connection"; state: CollaborationState["connection"] }
  | { type: "load.started"; incremental: boolean }
  | { type: "snapshot"; snapshot: CollaborationSnapshot }
  | { type: "event"; event: GatewayEvent }
  | { type: "error"; message: string }
  | { type: "clear" };

export function collaborationReducer(
  state: CollaborationState,
  action: CollaborationAction,
): CollaborationState {
  switch (action.type) {
    case "connection":
      return {
        ...state,
        connection: action.state,
        ...(action.state === "open" ? {} : { executionsById: {} }),
      };
    case "load.started":
      return {
        ...state,
        error: undefined,
        loading: !action.incremental,
        reconciling: action.incremental,
        executionsById: {},
      };
    case "snapshot":
      return applySnapshot(state, action.snapshot);
    case "event":
      return applyGatewayEvent(state, action.event);
    case "error":
      return { ...state, error: action.message, loading: false, reconciling: false };
    case "clear":
      return { ...initialCollaborationState, connection: state.connection };
  }
}

function applySnapshot(
  state: CollaborationState,
  snapshot: CollaborationSnapshot,
): CollaborationState {
  const incremental = snapshot.reconciliation.after_sequence > 0;
  const incomingEvents = Object.fromEntries(
    snapshot.events.map((event) => [event.sequence, event]),
  );
  const incomingEventIds = Object.fromEntries(
    snapshot.events.map((event) => [event.event_id, event.sequence]),
  );
  const mergedEvents = incremental ? mergeEvents(state, snapshot.events) : undefined;
  const eventsBySequence = mergedEvents?.eventsBySequence ?? incomingEvents;
  const eventSequenceById = mergedEvents?.eventSequenceById ?? incomingEventIds;

  return {
    ...state,
    approvalsById: byId(snapshot.approvals, "approval_id"),
    attachmentsById: byId(snapshot.attachments, "attachment_id"),
    error: undefined,
    eventsBySequence,
    eventSequenceById,
    executionsById: {},
    group: snapshot.group,
    lastSequence: Math.max(snapshot.group.last_sequence, snapshot.reconciliation.last_sequence),
    loading: false,
    membershipsById: byId(snapshot.memberships, "membership_id"),
    reconciling: false,
    targetsById: byId(snapshot.targets, "target_id"),
    turnsById: byId(snapshot.turns, "turn_id"),
  };
}

function applyGatewayEvent(state: CollaborationState, event: GatewayEvent): CollaborationState {
  if (!isCollaborationEvent(event) || !event.payload) return state;
  const payload = event.payload;
  const groupId = collaborationGroupId(event);
  if (state.group && groupId !== state.group.group_id) return state;

  switch (event.type) {
    case "collaboration.group.changed": {
      const group = payload as unknown as CollaborationState["group"];
      if (!group?.group_id || (state.group && group.group_id !== state.group.group_id)) return state;
      return { ...state, group, lastSequence: Math.max(state.lastSequence, group.last_sequence) };
    }
    case "collaboration.event.appended": {
      const appended = payload as unknown as CollaborationEvent;
      if (appended.sequence <= state.lastSequence || !validEventForState(state, appended)) return state;
      const merged = mergeEvents(state, [appended]);
      if (merged.eventsBySequence === state.eventsBySequence) return state;
      return {
        ...state,
        ...merged,
        lastSequence: Math.max(state.lastSequence, appended.sequence),
        group: state.group
          ? { ...state.group, last_sequence: Math.max(state.group.last_sequence, appended.sequence) }
          : state.group,
      };
    }
    case "collaboration.target.changed": {
      const target = payload as unknown as CollaborationTarget;
      if (!target.target_id || !target.execution_id || !target.turn_id) return state;
      const existing = state.targetsById[target.target_id];
      if (existing && targetVersion(target) < targetVersion(existing)) return state;
      if (existing && targetVersion(target) === targetVersion(existing)) {
        if (targetStatusRank(target.status) <= targetStatusRank(existing.status)) return state;
      }
      const executionsById = isTerminalTarget(target.status)
        ? omitKey(state.executionsById, target.execution_id)
        : state.executionsById;
      return {
        ...state,
        executionsById,
        targetsById: { ...state.targetsById, [target.target_id]: { ...existing, ...target } },
      };
    }
    case "collaboration.execution.delta": {
      const executionId = stringField(payload.execution_id);
      const targetId = stringField(payload.target_id);
      const turnId = stringField(payload.turn_id);
      const text = stringField(payload.text, true);
      if (!executionId || !targetId || !turnId || text === null) return state;
      const target = state.targetsById[targetId];
      if (target && (target.execution_id !== executionId || target.turn_id !== turnId)) return state;
      if (target && isTerminalTarget(target.status)) return state;
      return {
        ...state,
        executionsById: {
          ...state.executionsById,
          [executionId]: `${state.executionsById[executionId] ?? ""}${text}`,
        },
      };
    }
    case "collaboration.approval.changed": {
      const approval = payload as unknown as CollaborationApproval;
      if (!approval.approval_id || !approval.target_id || !approval.execution_id) return state;
      const existing = state.approvalsById[approval.approval_id];
      if (existing && approvalVersion(approval) < approvalVersion(existing)) return state;
      if (existing && approvalVersion(approval) === approvalVersion(existing)) {
        if (approvalStatusRank(approval.status) <= approvalStatusRank(existing.status)) return state;
      }
      return {
        ...state,
        approvalsById: {
          ...state.approvalsById,
          [approval.approval_id]: { ...existing, ...approval },
        },
      };
    }
  }
}

function validEventForState(state: CollaborationState, event: CollaborationEvent): boolean {
  if (!event.event_id || !Number.isInteger(event.sequence) || event.sequence <= 0) return false;
  if (state.group && event.group_id !== state.group.group_id) return false;
  const knownSequence = state.eventSequenceById[event.event_id];
  if (knownSequence !== undefined) return false;
  const existing = state.eventsBySequence[event.sequence];
  return existing === undefined;
}

function mergeEvents(
  state: Pick<CollaborationState, "eventsBySequence" | "eventSequenceById">,
  events: CollaborationEvent[],
): Pick<CollaborationState, "eventsBySequence" | "eventSequenceById"> {
  let eventsBySequence = state.eventsBySequence;
  let eventSequenceById = state.eventSequenceById;
  for (const event of [...events].sort((a, b) => a.sequence - b.sequence)) {
    if (!validEventForState({ ...state, eventsBySequence, eventSequenceById } as CollaborationState, event)) {
      continue;
    }
    if (eventsBySequence === state.eventsBySequence) eventsBySequence = { ...eventsBySequence };
    if (eventSequenceById === state.eventSequenceById) eventSequenceById = { ...eventSequenceById };
    eventsBySequence[event.sequence] = event;
    eventSequenceById[event.event_id] = event.sequence;
  }
  return { eventsBySequence, eventSequenceById };
}

function targetVersion(target: CollaborationTarget): number {
  return Number(target.updated_at ?? target.completed_at ?? target.created_at ?? 0);
}

function targetStatusRank(status: CollaborationTarget["status"]): number {
  if (isTerminalTarget(status)) return 3;
  if (status === "waiting_approval") return 2;
  if (status === "running") return 1;
  return 0;
}

function approvalVersion(approval: CollaborationApproval): number {
  return Number(approval.updated_at ?? approval.decided_at ?? approval.created_at ?? 0);
}

function approvalStatusRank(status: CollaborationApproval["status"]): number {
  return status === "pending" ? 0 : 1;
}

export function isTerminalTarget(status: CollaborationTarget["status"]): boolean {
  return ["completed", "failed", "timed_out", "ambiguous", "cancelled"].includes(status);
}

function stringField(value: unknown, allowEmpty = false): string | null {
  return typeof value === "string" && (allowEmpty || value.length > 0) ? value : null;
}

function byId<T, K extends keyof T>(values: T[], key: K): Record<string, T> {
  return Object.fromEntries(values.map((value) => [String(value[key]), value]));
}

function omitKey<T>(record: Record<string, T>, key: string): Record<string, T> {
  if (!(key in record)) return record;
  const next = { ...record };
  delete next[key];
  return next;
}
