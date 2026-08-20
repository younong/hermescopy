import { authedFetch, buildWsUrl, fetchJSON } from "./runtime";

import {
  KANBAN_DEFAULT_BOARD,
  type KanbanActiveWorker,
  type KanbanAttachment,
  type KanbanAutomationOutcome,
  type KanbanBoardMetadata,
  type KanbanBoardResponse,
  type KanbanBulkResult,
  type KanbanBulkTaskInput,
  type KanbanConfig,
  type KanbanCreateBoardInput,
  type KanbanCreateTaskInput,
  type KanbanDecomposeOutcome,
  type KanbanDiagnosticGroup,
  type KanbanDiagnosticSeverity,
  type KanbanDispatchResult,
  type KanbanEventEnvelope,
  type KanbanHomeChannel,
  type KanbanOrchestrationSettings,
  type KanbanOrchestrationUpdate,
  type KanbanProfile,
  type KanbanRun,
  type KanbanRunInspection,
  type KanbanRunStateType,
  type KanbanStats,
  type KanbanTask,
  type KanbanTaskDetailResponse,
  type KanbanUpdateBoardInput,
  type KanbanUpdateTaskInput,
  type KanbanWorkerLog,
  type KanbanAssignee,
} from "./types";

const BASE = "/api/plugins/kanban";

export interface KanbanBoardFilters {
  tenant?: string;
  include_archived?: boolean;
  workflow_template_id?: string;
  current_step_key?: string;
}

export interface KanbanApi {
  listBoards(includeArchived?: boolean): Promise<{ boards: KanbanBoardMetadata[]; current: string }>;
  createBoard(input: KanbanCreateBoardInput): Promise<{ board: KanbanBoardMetadata; current: string }>;
  updateBoard(slug: string, input: KanbanUpdateBoardInput): Promise<{ board: KanbanBoardMetadata }>;
  removeBoard(slug: string, hardDelete?: boolean): Promise<{ result: Record<string, unknown>; current: string }>;
  switchBoard(slug: string): Promise<{ current: string }>;
  getBoard(board: string, filters?: KanbanBoardFilters, signal?: AbortSignal): Promise<KanbanBoardResponse>;
  getTask(board: string, taskId: string, runState?: { type: KanbanRunStateType; name: string }, signal?: AbortSignal): Promise<KanbanTaskDetailResponse>;
  createTask(board: string, input: KanbanCreateTaskInput): Promise<{ task: KanbanTask | null; warning?: string }>;
  updateTask(board: string, taskId: string, input: KanbanUpdateTaskInput): Promise<{ task: KanbanTask | null }>;
  deleteTask(board: string, taskId: string): Promise<{ deleted: boolean; task_id: string }>;
  bulkUpdate(board: string, input: KanbanBulkTaskInput): Promise<{ results: KanbanBulkResult[] }>;
  addComment(board: string, taskId: string, body: string, author?: string | null): Promise<{ ok: boolean }>;
  addLink(board: string, parentId: string, childId: string): Promise<{ ok: boolean }>;
  deleteLink(board: string, parentId: string, childId: string): Promise<{ ok: boolean }>;
  listAttachments(board: string, taskId: string): Promise<{ attachments: KanbanAttachment[] }>;
  uploadAttachment(board: string, taskId: string, file: File, uploadedBy?: string): Promise<{ attachment: KanbanAttachment | null }>;
  downloadAttachment(board: string, attachmentId: number, signal?: AbortSignal): Promise<Response>;
  deleteAttachment(board: string, attachmentId: number): Promise<{ ok: boolean; id: number }>;
  getStats(board: string): Promise<KanbanStats>;
  getAssignees(board: string): Promise<{ assignees: KanbanAssignee[] }>;
  dispatch(board: string, options?: { dryRun?: boolean; max?: number }): Promise<KanbanDispatchResult>;
  getDiagnostics(board: string, severity?: KanbanDiagnosticSeverity): Promise<{ diagnostics: KanbanDiagnosticGroup[]; count: number }>;
  listActiveWorkers(board: string): Promise<{ workers: KanbanActiveWorker[]; count: number; checked_at: number }>;
  getRun(board: string, runId: number): Promise<{ run: KanbanRun }>;
  inspectRun(board: string, runId: number): Promise<KanbanRunInspection>;
  terminateRun(board: string, runId: number, reason?: string | null): Promise<{ ok: boolean; run_id: number; task_id: string }>;
  getTaskLog(board: string, taskId: string, tail?: number): Promise<KanbanWorkerLog>;
  reclaimTask(board: string, taskId: string, reason?: string | null): Promise<{ ok: boolean; task_id: string }>;
  reassignTask(board: string, taskId: string, input: { profile?: string | null; reclaim_first?: boolean; reason?: string | null }): Promise<{ ok: boolean; task_id: string; assignee: string | null }>;
  specifyTask(board: string, taskId: string, author?: string | null): Promise<KanbanAutomationOutcome>;
  decomposeTask(board: string, taskId: string, author?: string | null): Promise<KanbanDecomposeOutcome>;
  getHomeChannels(board: string, taskId?: string): Promise<{ home_channels: KanbanHomeChannel[] }>;
  subscribeHome(board: string, taskId: string, platform: string): Promise<{ ok: boolean; task_id: string; home_channel: KanbanHomeChannel }>;
  unsubscribeHome(board: string, taskId: string, platform: string): Promise<{ ok: boolean; task_id: string; home_channel: KanbanHomeChannel }>;
  getConfig(): Promise<KanbanConfig>;
  listProfiles(): Promise<{ profiles: KanbanProfile[] }>;
  updateProfileDescription(profileName: string, description?: string | null): Promise<{ ok: boolean; profile: string; description: string }>;
  autoDescribeProfile(profileName: string, overwrite?: boolean): Promise<{ ok: boolean; profile: string; reason: string | null; description: string | null }>;
  getOrchestration(): Promise<KanbanOrchestrationSettings>;
  updateOrchestration(input: KanbanOrchestrationUpdate): Promise<KanbanOrchestrationSettings>;
  buildEventsUrl(board: string, since: number, signal?: AbortSignal): Promise<string>;
}

function encodePath(value: string): string {
  return encodeURIComponent(value);
}

function boardParams(board: string, extra?: Record<string, string | number | boolean | undefined>): string {
  const params = new URLSearchParams({ board: board || KANBAN_DEFAULT_BOARD });
  for (const [key, value] of Object.entries(extra ?? {})) {
    if (value !== undefined) params.set(key, String(value));
  }
  return params.toString();
}

function jsonInit(method: string, body?: unknown, signal?: AbortSignal): RequestInit {
  return {
    method,
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  };
}

async function checkedResponse(response: Response): Promise<Response> {
  if (!response.ok) {
    const text = await response.text().catch(() => response.statusText);
    throw new Error(`${response.status}: ${text}`);
  }
  return response;
}

export const kanbanApi: KanbanApi = {
  listBoards: (includeArchived = false) =>
    fetchJSON(`${BASE}/boards?include_archived=${String(includeArchived)}`),
  createBoard: (input) => fetchJSON(`${BASE}/boards`, jsonInit("POST", input)),
  updateBoard: (slug, input) =>
    fetchJSON(`${BASE}/boards/${encodePath(slug)}`, jsonInit("PATCH", input)),
  removeBoard: (slug, hardDelete = false) =>
    fetchJSON(`${BASE}/boards/${encodePath(slug)}?delete=${String(hardDelete)}`, { method: "DELETE" }),
  switchBoard: (slug) =>
    fetchJSON(`${BASE}/boards/${encodePath(slug)}/switch`, { method: "POST" }),

  getBoard: (board, filters = {}, signal) =>
    fetchJSON(`${BASE}/board?${boardParams(board, { ...filters })}`, { signal }),
  getTask: (board, taskId, runState, signal) =>
    fetchJSON(
      `${BASE}/tasks/${encodePath(taskId)}?${boardParams(board, runState ? {
        run_state_type: runState.type,
        run_state_name: runState.name,
      } : undefined)}`,
      { signal },
    ),
  createTask: (board, input) =>
    fetchJSON(`${BASE}/tasks?${boardParams(board)}`, jsonInit("POST", input)),
  updateTask: (board, taskId, input) =>
    fetchJSON(`${BASE}/tasks/${encodePath(taskId)}?${boardParams(board)}`, jsonInit("PATCH", input)),
  deleteTask: (board, taskId) =>
    fetchJSON(`${BASE}/tasks/${encodePath(taskId)}?${boardParams(board)}`, { method: "DELETE" }),
  bulkUpdate: (board, input) =>
    fetchJSON(`${BASE}/tasks/bulk?${boardParams(board)}`, jsonInit("POST", input)),
  addComment: (board, taskId, body, author) =>
    fetchJSON(
      `${BASE}/tasks/${encodePath(taskId)}/comments?${boardParams(board)}`,
      jsonInit("POST", { body, ...(author === undefined ? {} : { author }) }),
    ),
  addLink: (board, parentId, childId) =>
    fetchJSON(`${BASE}/links?${boardParams(board)}`, jsonInit("POST", { parent_id: parentId, child_id: childId })),
  deleteLink: (board, parentId, childId) =>
    fetchJSON(`${BASE}/links?${boardParams(board, { parent_id: parentId, child_id: childId })}`, { method: "DELETE" }),

  listAttachments: (board, taskId) =>
    fetchJSON(`${BASE}/tasks/${encodePath(taskId)}/attachments?${boardParams(board)}`),
  uploadAttachment: (board, taskId, file, uploadedBy) => {
    const form = new FormData();
    form.append("file", file, file.name);
    if (uploadedBy !== undefined) form.append("uploaded_by", uploadedBy);
    return fetchJSON(`${BASE}/tasks/${encodePath(taskId)}/attachments?${boardParams(board)}`, {
      method: "POST",
      body: form,
    });
  },
  downloadAttachment: async (board, attachmentId, signal) =>
    checkedResponse(await authedFetch(`${BASE}/attachments/${attachmentId}?${boardParams(board)}`, { signal })),
  deleteAttachment: (board, attachmentId) =>
    fetchJSON(`${BASE}/attachments/${attachmentId}?${boardParams(board)}`, { method: "DELETE" }),

  getStats: (board) => fetchJSON(`${BASE}/stats?${boardParams(board)}`),
  getAssignees: (board) => fetchJSON(`${BASE}/assignees?${boardParams(board)}`),
  dispatch: (board, options = {}) =>
    fetchJSON(`${BASE}/dispatch?${boardParams(board, {
      dry_run: options.dryRun ?? false,
      max: options.max ?? 8,
    })}`, { method: "POST" }),

  getDiagnostics: (board, severity) =>
    fetchJSON(`${BASE}/diagnostics?${boardParams(board, { severity })}`),
  listActiveWorkers: (board) => fetchJSON(`${BASE}/workers/active?${boardParams(board)}`),
  getRun: (board, runId) => fetchJSON(`${BASE}/runs/${runId}?${boardParams(board)}`),
  inspectRun: (board, runId) => fetchJSON(`${BASE}/runs/${runId}/inspect?${boardParams(board)}`),
  terminateRun: (board, runId, reason) =>
    fetchJSON(`${BASE}/runs/${runId}/terminate?${boardParams(board)}`, jsonInit("POST", { reason: reason ?? null })),
  getTaskLog: (board, taskId, tail) =>
    fetchJSON(`${BASE}/tasks/${encodePath(taskId)}/log?${boardParams(board, { tail })}`),
  reclaimTask: (board, taskId, reason) =>
    fetchJSON(`${BASE}/tasks/${encodePath(taskId)}/reclaim?${boardParams(board)}`, jsonInit("POST", { reason: reason ?? null })),
  reassignTask: (board, taskId, input) =>
    fetchJSON(`${BASE}/tasks/${encodePath(taskId)}/reassign?${boardParams(board)}`, jsonInit("POST", input)),
  specifyTask: (board, taskId, author) =>
    fetchJSON(`${BASE}/tasks/${encodePath(taskId)}/specify?${boardParams(board)}`, jsonInit("POST", { author: author ?? null })),
  decomposeTask: (board, taskId, author) =>
    fetchJSON(`${BASE}/tasks/${encodePath(taskId)}/decompose?${boardParams(board)}`, jsonInit("POST", { author: author ?? null })),

  getHomeChannels: (board, taskId) =>
    fetchJSON(`${BASE}/home-channels?${boardParams(board, { task_id: taskId })}`),
  subscribeHome: (board, taskId, platform) =>
    fetchJSON(`${BASE}/tasks/${encodePath(taskId)}/home-subscribe/${encodePath(platform)}?${boardParams(board)}`, { method: "POST" }),
  unsubscribeHome: (board, taskId, platform) =>
    fetchJSON(`${BASE}/tasks/${encodePath(taskId)}/home-subscribe/${encodePath(platform)}?${boardParams(board)}`, { method: "DELETE" }),

  getConfig: () => fetchJSON(`${BASE}/config`),
  listProfiles: () => fetchJSON(`${BASE}/profiles`),
  updateProfileDescription: (profileName, description) =>
    fetchJSON(`${BASE}/profiles/${encodePath(profileName)}`, jsonInit("PATCH", { description: description ?? null })),
  autoDescribeProfile: (profileName, overwrite = false) =>
    fetchJSON(`${BASE}/profiles/${encodePath(profileName)}/describe-auto`, jsonInit("POST", { overwrite })),
  getOrchestration: () => fetchJSON(`${BASE}/orchestration`),
  updateOrchestration: (input) =>
    fetchJSON(`${BASE}/orchestration`, jsonInit("PUT", input)),
  buildEventsUrl: (board, since, signal) =>
    buildWsUrl(`${BASE}/events`, {
      board: board || KANBAN_DEFAULT_BOARD,
      since: String(since),
    }, { signal }),
};

export function parseKanbanEventEnvelope(data: unknown): KanbanEventEnvelope | null {
  if (typeof data !== "string") return null;
  try {
    const value = JSON.parse(data) as Partial<KanbanEventEnvelope>;
    if (!Array.isArray(value.events) || typeof value.cursor !== "number") return null;
    return value as KanbanEventEnvelope;
  } catch {
    return null;
  }
}
