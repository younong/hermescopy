import type { ConnectionState } from "@/lib/gatewayClient";

export type CollaborationGroupStatus = "active" | "archived";
export type CollaborationTurnStatus =
  | "queued"
  | "running"
  | "completed"
  | "partial"
  | "failed"
  | "ambiguous"
  | "cancelled";
export type CollaborationTargetStatus =
  | "queued"
  | "running"
  | "waiting_approval"
  | "completed"
  | "failed"
  | "timed_out"
  | "ambiguous"
  | "cancelled";
export type CollaborationApprovalStatus = "pending" | "approved" | "denied" | "ambiguous";
export type CollaborationApprovalChoice = "once" | "session" | "always" | "deny";

export interface CollaborationGroup {
  group_id: string;
  name: string;
  creator_kind: "owner" | "employee";
  creator_employee_id: string | null;
  status: CollaborationGroupStatus;
  last_sequence: number;
  created_at: number;
  updated_at: number;
  archived_at: number | null;
}

export interface CollaborationMembership {
  membership_id: string;
  group_id: string;
  employee_id: string;
  profile_revision: number;
  profile_fingerprint: string;
  role: string;
  join_sequence: number;
  leave_sequence: number | null;
  created_at: number;
  left_at: number | null;
}

export interface CollaborationEventBody {
  text?: string;
  mentions?: string[];
  mention_all?: boolean;
  target_id?: string;
  employee_id?: string;
  membership_id?: string;
  name?: string;
  discussion_id?: string;
  discussion_round?: number;
  total_rounds?: number;
  [key: string]: unknown;
}

export interface CollaborationEvent {
  event_id: string;
  group_id: string;
  sequence: number;
  event_kind: string;
  actor_kind: "owner" | "employee" | "system";
  actor_employee_id: string | null;
  actor_membership_id: string | null;
  body: CollaborationEventBody;
  created_at: number;
}

export interface CollaborationTurn {
  turn_id: string;
  group_id: string;
  event_id: string;
  snapshot_sequence: number;
  status: CollaborationTurnStatus;
  created_at?: number;
  updated_at?: number;
  completed_at?: number | null;
}

export interface CollaborationTargetResult {
  text?: string;
  status?: string;
  event_id?: string;
  [key: string]: unknown;
}

export interface CollaborationTarget {
  target_id: string;
  execution_id: string;
  turn_id: string;
  employee_id: string;
  membership_id: string;
  join_sequence?: number;
  snapshot_sequence: number;
  status: CollaborationTargetStatus;
  error: string | null;
  result: CollaborationTargetResult | null;
  last_delivered_sequence?: number;
  active_seconds: number;
  active_started_at?: number | null;
  attempt: number;
  created_at?: number;
  updated_at?: number;
  completed_at?: number | null;
}

export interface CollaborationApprovalRequest {
  summary?: string;
  description?: string;
  tool_name?: string;
  allow_permanent?: boolean;
}

export interface CollaborationApproval {
  approval_id: string;
  group_id: string;
  turn_id: string;
  target_id: string;
  execution_id: string;
  tool_name?: string;
  status: CollaborationApprovalStatus;
  request?: CollaborationApprovalRequest;
  created_at?: number;
  updated_at?: number;
  decided_at?: number | null;
}

export interface CollaborationAttachment {
  attachment_id: string;
  group_id: string;
  event_id: string | null;
  filename: string;
  media_type: string;
  size_bytes: number;
  content_sha256: string;
  created_at: number;
}

export interface CollaborationReconciliation {
  after_sequence: number;
  last_sequence: number;
  next_after_sequence: number;
  snapshot_authoritative: true;
}

export type CollaborationHistoryDirection = "initial" | "backward" | "forward";

export interface CollaborationHistoryPage {
  direction: CollaborationHistoryDirection;
  limit: number;
  snapshot_sequence: number;
  range_start_sequence: number | null;
  range_end_sequence: number | null;
  before_sequence: number | null;
  next_before_sequence: number | null;
  after_sequence: number | null;
  next_after_sequence: number | null;
  through_sequence: number;
  has_more: boolean;
}

export interface CollaborationGetOptions {
  limit?: number;
  before_sequence?: number;
  after_sequence?: number;
  through_sequence?: number;
  reconcile_membership_ids?: string[];
  reconcile_target_ids?: string[];
  reconcile_approval_ids?: string[];
}

export interface CollaborationSnapshot {
  group: CollaborationGroup;
  memberships: CollaborationMembership[];
  events: CollaborationEvent[];
  turns: CollaborationTurn[];
  targets: CollaborationTarget[];
  approvals: CollaborationApproval[];
  attachments: CollaborationAttachment[];
  /** Absent only while an older server remains in a rolling deployment. */
  history_page?: CollaborationHistoryPage;
  reconciliation?: CollaborationReconciliation;
}

export interface CollaborationState {
  group?: CollaborationGroup;
  connection: ConnectionState;
  membershipsById: Record<string, CollaborationMembership>;
  eventsBySequence: Record<number, CollaborationEvent>;
  eventSequenceById: Record<string, number>;
  turnsById: Record<string, CollaborationTurn>;
  targetsById: Record<string, CollaborationTarget>;
  executionsById: Record<string, string>;
  approvalsById: Record<string, CollaborationApproval>;
  attachmentsById: Record<string, CollaborationAttachment>;
  lastSequence: number;
  reconciledSequence: number;
  historyBeforeSequence?: number;
  historyHasMore: boolean;
  historyLoading: boolean;
  historyError?: string;
  loading: boolean;
  reconciling: boolean;
  error?: string;
}

export const initialCollaborationState: CollaborationState = {
  approvalsById: {},
  attachmentsById: {},
  connection: "idle",
  eventSequenceById: {},
  eventsBySequence: {},
  executionsById: {},
  historyHasMore: false,
  historyLoading: false,
  lastSequence: 0,
  reconciledSequence: 0,
  loading: true,
  membershipsById: {},
  reconciling: false,
  targetsById: {},
  turnsById: {},
};

export interface CollaborationSubmitMessage {
  group_id: string;
  text: string;
  mentioned_membership_ids: string[];
  mention_all: boolean;
  client_idempotency_key: string;
  attachment_ids: string[];
}

export interface CollaborationEmployeeIdentity {
  employeeId: string;
  name: string;
  role?: string;
  avatarUrl?: string;
  available: boolean;
}
