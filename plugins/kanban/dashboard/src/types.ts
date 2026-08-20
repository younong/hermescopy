export const KANBAN_DEFAULT_BOARD = "default";

export type KanbanStatus =
  | "triage"
  | "todo"
  | "scheduled"
  | "ready"
  | "running"
  | "blocked"
  | "review"
  | "done"
  | "archived";

export type KanbanDiagnosticSeverity = "warning" | "error" | "critical";
export type KanbanRunStateType = "status" | "outcome";
export type KanbanMetadata = Record<string, unknown>;

export interface KanbanAge {
  created_age_seconds: number | null;
  started_age_seconds: number | null;
  time_to_complete_seconds: number | null;
}

export interface KanbanDiagnosticAction {
  kind: string;
  label: string;
  payload: KanbanMetadata;
  suggested: boolean;
}

export interface KanbanDiagnostic {
  kind: string;
  severity: KanbanDiagnosticSeverity;
  title: string;
  detail: string;
  actions: KanbanDiagnosticAction[];
  first_seen_at: number;
  last_seen_at: number;
  count: number;
  run_id: number | null;
  data: KanbanMetadata;
}

export interface KanbanWarningsSummary {
  count: number;
  kinds: Record<string, number>;
  latest_at: number;
  highest_severity: KanbanDiagnosticSeverity | null;
}

export interface KanbanWorkflowStep {
  key: string;
  assignee: string;
}

export interface KanbanWorkflow {
  steps: KanbanWorkflowStep[];
  auto_advance: boolean;
}

export interface KanbanTask {
  id: string;
  title: string;
  body: string | null;
  assignee: string | null;
  status: string;
  priority: number;
  created_by: string | null;
  created_at: number;
  started_at: number | null;
  completed_at: number | null;
  workspace_kind: string;
  workspace_path: string | null;
  claim_lock: string | null;
  claim_expires: number | null;
  tenant: string | null;
  branch_name: string | null;
  project_id: string | null;
  result: string | null;
  idempotency_key: string | null;
  consecutive_failures: number;
  worker_pid: number | null;
  last_failure_error: string | null;
  max_runtime_seconds: number | null;
  last_heartbeat_at: number | null;
  current_run_id: number | null;
  workflow_template_id: string | null;
  current_step_key: string | null;
  workflow: KanbanWorkflow | null;
  skills: string[] | null;
  model_override: string | null;
  max_retries: number | null;
  goal_mode: boolean;
  goal_max_turns: number | null;
  session_id: string | null;
  block_kind: string | null;
  block_recurrences: number;
  age: KanbanAge;
  latest_summary: string | null;
  link_counts?: { parents: number; children: number };
  comment_count?: number;
  progress?: { done: number; total: number } | null;
  diagnostics?: KanbanDiagnostic[];
  warnings?: KanbanWarningsSummary;
}

export interface KanbanColumn {
  name: string;
  tasks: KanbanTask[];
}

export interface KanbanBoardResponse {
  columns: KanbanColumn[];
  tenants: string[];
  assignees: string[];
  latest_event_id: number;
  now: number;
}

export interface KanbanEvent {
  id: number;
  task_id: string;
  run_id: number | null;
  kind: string;
  payload: KanbanMetadata | null;
  created_at: number;
}

export interface KanbanComment {
  id: number;
  task_id: string;
  author: string;
  body: string;
  created_at: number;
}

export interface KanbanAttachment {
  id: number;
  task_id: string;
  filename: string;
  content_type: string | null;
  size: number;
  uploaded_by: string | null;
  stored_path: string;
  created_at: number;
}

export interface KanbanRun {
  id: number;
  task_id: string;
  profile: string | null;
  step_key: string | null;
  status: string;
  claim_lock: string | null;
  claim_expires: number | null;
  worker_pid: number | null;
  max_runtime_seconds: number | null;
  last_heartbeat_at: number | null;
  started_at: number;
  ended_at: number | null;
  outcome: string | null;
  summary: string | null;
  metadata: KanbanMetadata | null;
  error: string | null;
}

export interface KanbanTaskDetailResponse {
  task: KanbanTask;
  comments: KanbanComment[];
  events: KanbanEvent[];
  attachments: KanbanAttachment[];
  links: { parents: string[]; children: string[] };
  runs: KanbanRun[];
}

export interface KanbanCreateTaskInput {
  title: string;
  body?: string | null;
  assignee?: string | null;
  tenant?: string | null;
  priority?: number;
  workspace_kind?: string;
  workspace_path?: string | null;
  parents?: string[];
  triage?: boolean;
  idempotency_key?: string | null;
  max_runtime_seconds?: number | null;
  skills?: string[] | null;
  goal_mode?: boolean;
  goal_max_turns?: number | null;
  workflow?: KanbanWorkflow | null;
}

export interface KanbanUpdateTaskInput {
  status?: string;
  assignee?: string | null;
  priority?: number;
  title?: string;
  body?: string;
  result?: string | null;
  block_reason?: string | null;
  summary?: string | null;
  metadata?: KanbanMetadata | null;
  workflow?: KanbanWorkflow | null;
}

export interface KanbanBulkTaskInput {
  ids: string[];
  status?: string;
  assignee?: string | null;
  priority?: number;
  archive?: boolean;
  result?: string | null;
  summary?: string | null;
  metadata?: KanbanMetadata | null;
  reclaim_first?: boolean;
}

export interface KanbanBulkResult {
  id: string;
  ok: boolean;
  error?: string;
}

export interface KanbanDiagnosticGroup {
  task_id: string;
  task_title: string | null;
  task_status: string | null;
  task_assignee: string | null;
  diagnostics: KanbanDiagnostic[];
}

export interface KanbanActiveWorker {
  run_id: number;
  task_id: string;
  task_title: string;
  task_status: string;
  task_assignee: string | null;
  profile: string | null;
  worker_pid: number;
  started_at: number;
  claim_lock: string | null;
  claim_expires: number | null;
  last_heartbeat_at: number | null;
  max_runtime_seconds: number | null;
}

export interface KanbanRunInspection {
  run_id: number;
  alive: boolean;
  pid?: number;
  reason?: string;
  error?: string;
  cpu_percent?: number | null;
  memory_rss_bytes?: number | null;
  memory_vms_bytes?: number | null;
  num_threads?: number | null;
  num_fds?: number | null;
  status?: string | null;
  create_time?: number | null;
  cmdline?: string[] | null;
}

export interface KanbanHomeChannel {
  platform: string;
  chat_id: string;
  thread_id: string;
  name: string;
  subscribed: boolean;
}

export interface KanbanStats {
  by_status: Record<string, number>;
  by_assignee: Record<string, Record<string, number>>;
  oldest_ready_age_seconds: number | null;
  now: number;
}

export interface KanbanAssignee {
  name: string;
  counts?: Record<string, number>;
  [key: string]: unknown;
}

export interface KanbanWorkerLog {
  task_id: string;
  path: string;
  exists: boolean;
  size_bytes: number;
  content: string;
  truncated: boolean;
}

export interface KanbanDispatchResult {
  reclaimed: number;
  promoted: number;
  spawned: Array<[string, string, string]>;
  skipped_unassigned: string[];
  auto_assigned_default: string[];
  skipped_nonspawnable: string[];
  skipped_per_profile_capped: Array<[string, string, number]>;
  crashed: string[];
  auto_blocked: string[];
  timed_out: string[];
  stale: string[];
  respawn_guarded: Array<[string, string]>;
  rate_limited: string[];
  skipped_locked: boolean;
  result?: string;
}

export interface KanbanBoardMetadata {
  slug: string;
  name: string;
  description: string;
  icon: string;
  color: string;
  default_workdir: string | null;
  created_at: number | null;
  archived: boolean;
  db_path: string;
  is_current?: boolean;
  counts?: Record<string, number>;
  total?: number;
}

export interface KanbanCreateBoardInput {
  slug: string;
  name?: string | null;
  description?: string | null;
  icon?: string | null;
  color?: string | null;
  switch?: boolean;
}

export interface KanbanUpdateBoardInput {
  name?: string | null;
  description?: string | null;
  icon?: string | null;
  color?: string | null;
}

export interface KanbanProfile {
  name: string;
  is_default: boolean;
  model: string;
  provider: string;
  description: string;
  description_auto: boolean;
  skill_count: number;
}

export interface KanbanOrchestrationSettings {
  orchestrator_profile: string;
  default_assignee: string;
  auto_decompose: boolean;
  auto_promote_children: boolean;
  resolved_orchestrator_profile: string;
  resolved_default_assignee: string;
  active_profile: string;
}

export interface KanbanOrchestrationUpdate {
  orchestrator_profile?: string | null;
  default_assignee?: string | null;
  auto_decompose?: boolean;
  auto_promote_children?: boolean;
}

export interface KanbanAutomationOutcome {
  ok: boolean;
  task_id: string;
  reason: string | null;
  new_title: string | null;
}

export interface KanbanDecomposeOutcome extends KanbanAutomationOutcome {
  fanout: boolean;
  child_ids: string[];
}

export interface KanbanConfig {
  default_tenant: string;
  lane_by_profile: boolean;
  include_archived_by_default: boolean;
  render_markdown: boolean;
}

export interface KanbanEventEnvelope {
  events: KanbanEvent[];
  cursor: number;
}

export type KanbanConnectionState = "idle" | "connecting" | "connected" | "reconnecting";
