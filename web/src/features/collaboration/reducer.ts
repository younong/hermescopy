import type { GatewayEvent } from "@/lib/gatewayClient";
import { collaborationGroupId, isCollaborationEvent } from "./protocol";
import type {
  CollaborationApproval,
  CollaborationEvent,
  CollaborationHistoryDirection,
  CollaborationSnapshot,
  CollaborationState,
  CollaborationTarget,
} from "./types";
import { initialCollaborationState } from "./types";

export type CollaborationAction =
  | { type: "connection"; state: CollaborationState["connection"] }
  | { type: "load.started"; mode: CollaborationHistoryDirection }
  | { type: "snapshot"; snapshot: CollaborationSnapshot }
  | { type: "event"; event: GatewayEvent }
  | { type: "load.failed"; mode: CollaborationHistoryDirection; message: string }
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
      return action.mode === "backward"
        ? { ...state, historyError: undefined, historyLoading: true }
        : {
          ...state,
          error: undefined,
          loading: action.mode === "initial",
          reconciling: action.mode === "forward",
        };
    case "snapshot":
      return applySnapshot(state, action.snapshot);
    case "event":
      return applyGatewayEvent(state, action.event);
    case "load.failed":
      return action.mode === "backward"
        ? { ...state, historyError: action.message, historyLoading: false }
        : { ...state, error: action.message, loading: false, reconciling: false };
    case "clear":
      return { ...initialCollaborationState, connection: state.connection };
  }
}

function applySnapshot(
  state: CollaborationState,
  snapshot: CollaborationSnapshot,
): CollaborationState {
  const direction = snapshot.history_page?.direction
    ?? ((snapshot.reconciliation?.after_sequence ?? 0) > 0 ? "forward" : "initial");
  const snapshotSequence = snapshot.history_page?.snapshot_sequence
    ?? snapshot.reconciliation?.last_sequence
    ?? snapshot.group.last_sequence;
  const merged = direction === "initial"
    ? replaceInitialEvents(state, snapshot, snapshotSequence)
    : mergeEvents(state, snapshot.events);
  const nextBefore = snapshot.history_page?.next_before_sequence ?? undefined;
  const hasMore = snapshot.history_page?.has_more ?? false;
  const updatesHistory = direction !== "forward";
  const reconciliationComplete = direction === "initial" || (direction === "forward" && !hasMore);

  return {
    ...state,
    attachmentsById: mergeEntityPage(state.attachmentsById, snapshot.attachments, "attachment_id", direction),
    error: undefined,
    eventsBySequence: merged.eventsBySequence,
    eventSequenceById: merged.eventSequenceById,
    group: snapshot.group,
    historyBeforeSequence: updatesHistory ? nextBefore : state.historyBeforeSequence,
    historyError: updatesHistory ? undefined : state.historyError,
    historyHasMore: updatesHistory ? hasMore : state.historyHasMore,
    historyLoading: updatesHistory ? false : state.historyLoading,
    lastSequence: Math.max(state.lastSequence, snapshot.group.last_sequence, snapshotSequence),
    reconciledSequence: reconciliationComplete
      ? snapshot.history_page?.through_sequence ?? snapshot.reconciliation?.next_after_sequence ?? snapshotSequence
      : state.reconciledSequence,
    loading: false,
    membershipsById: mergeEntityPage(state.membershipsById, snapshot.memberships, "membership_id", direction),
    reconciling: false,
    targetsById: mergeVersionedTargets(state.targetsById, snapshot.targets, direction),
    turnsById: mergeEntityPage(state.turnsById, snapshot.turns, "turn_id", direction),
    approvalsById: mergeVersionedApprovals(state.approvalsById, snapshot.approvals, direction),
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
      if (!validEventForState(state, appended)) return state;
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
      if (existing && !preferApproval(approval, existing)) return state;
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

function replaceInitialEvents(
  state: Pick<CollaborationState, "group" | "eventsBySequence" | "eventSequenceById">,
  snapshot: CollaborationSnapshot,
  snapshotSequence: number,
) {
  const liveEvents = Object.values(state.eventsBySequence)
    .filter((event) => event.group_id === snapshot.group.group_id && event.sequence > snapshotSequence);
  const events = [...snapshot.events, ...liveEvents];
  return {
    eventsBySequence: Object.fromEntries(events.map((event) => [event.sequence, event])),
    eventSequenceById: Object.fromEntries(events.map((event) => [event.event_id, event.sequence])),
  };
}

function validEventForState(state: Pick<CollaborationState, "group" | "eventsBySequence" | "eventSequenceById">, event: CollaborationEvent): boolean {
  if (!event.event_id || !Number.isInteger(event.sequence) || event.sequence <= 0) return false;
  if (state.group && event.group_id !== state.group.group_id) return false;
  return state.eventSequenceById[event.event_id] === undefined && state.eventsBySequence[event.sequence] === undefined;
}

function mergeEvents(
  state: Pick<CollaborationState, "group" | "eventsBySequence" | "eventSequenceById">,
  events: CollaborationEvent[],
): Pick<CollaborationState, "eventsBySequence" | "eventSequenceById"> {
  let eventsBySequence = state.eventsBySequence;
  let eventSequenceById = state.eventSequenceById;
  for (const event of [...events].sort((a, b) => a.sequence - b.sequence)) {
    if (!validEventForState({ ...state, eventsBySequence, eventSequenceById }, event)) continue;
    if (eventsBySequence === state.eventsBySequence) eventsBySequence = { ...eventsBySequence };
    if (eventSequenceById === state.eventSequenceById) eventSequenceById = { ...eventSequenceById };
    eventsBySequence[event.sequence] = event;
    eventSequenceById[event.event_id] = event.sequence;
  }
  return { eventsBySequence, eventSequenceById };
}

function mergeEntityPage<T, K extends keyof T>(
  current: Record<string, T>,
  values: T[],
  key: K,
  direction: CollaborationHistoryDirection,
): Record<string, T> {
  const incoming = byId(values, key);
  return direction === "initial" ? incoming : { ...current, ...incoming };
}

function mergeVersionedTargets(
  current: Record<string, CollaborationTarget>,
  values: CollaborationTarget[],
  direction: CollaborationHistoryDirection,
) {
  if (direction === "initial") return byId(values, "target_id");
  const next = { ...current };
  for (const target of values) {
    const existing = next[target.target_id];
    if (!existing || targetVersion(target) > targetVersion(existing)
      || (targetVersion(target) === targetVersion(existing) && targetStatusRank(target.status) > targetStatusRank(existing.status))) {
      next[target.target_id] = { ...existing, ...target };
    }
  }
  return next;
}

function mergeVersionedApprovals(
  current: Record<string, CollaborationApproval>,
  values: CollaborationApproval[],
  direction: CollaborationHistoryDirection,
) {
  if (direction === "initial") return byId(values, "approval_id");
  const next = { ...current };
  for (const approval of values) {
    const existing = next[approval.approval_id];
    if (!existing || preferApproval(approval, existing)) next[approval.approval_id] = { ...existing, ...approval };
  }
  return next;
}

function preferApproval(incoming: CollaborationApproval, existing: CollaborationApproval): boolean {
  const incomingVersion = approvalVersion(incoming);
  const existingVersion = approvalVersion(existing);
  return incomingVersion > existingVersion
    || (incomingVersion === existingVersion && approvalStatusRank(incoming.status) > approvalStatusRank(existing.status));
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
