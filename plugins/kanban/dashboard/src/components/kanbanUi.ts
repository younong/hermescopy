import type { PluginTranslations } from "../translations";
import type { KanbanStatus, KanbanTask, KanbanUpdateTaskInput } from "../types";

export const KANBAN_STATUSES: KanbanStatus[] = [
  "triage",
  "todo",
  "scheduled",
  "ready",
  "running",
  "blocked",
  "review",
  "done",
  "archived",
];

export const MOVABLE_STATUSES = KANBAN_STATUSES.filter((status) => status !== "running");

export function statusLabel(t: PluginTranslations, status: string): string {
  const labels = t.kanban.columnLabels as Record<string, string | undefined>;
  return labels[status] ?? status;
}

export function statusHelp(t: PluginTranslations, status: string): string {
  const help = t.kanban.columnHelp as Record<string, string | undefined>;
  return help[status] ?? "";
}

export function allTasks(columns: Array<{ tasks: KanbanTask[] }>): KanbanTask[] {
  return columns.flatMap((column) => column.tasks);
}

export function taskMatches(task: KanbanTask, normalizedQuery: string, assignee: string): boolean {
  if (assignee && (task.assignee ?? "") !== assignee) return false;
  if (!normalizedQuery) return true;
  return [task.id, task.title, task.body, task.assignee, task.tenant, task.latest_summary]
    .some((value) => String(value ?? "").toLocaleLowerCase().includes(normalizedQuery));
}

export function transitionNeedsInput(status: string): "summary" | "reason" | null {
  if (status === "done") return "summary";
  if (status === "blocked" || status === "scheduled") return "reason";
  return null;
}

export function transitionPayload(status: string, detail = ""): KanbanUpdateTaskInput {
  if (status === "done") return { status, result: detail, summary: detail };
  if (status === "blocked" || status === "scheduled") {
    return { status, block_reason: detail };
  }
  return { status };
}

export function formatEpoch(value: number | null | undefined, locale: string): string {
  if (!value) return "—";
  return new Date(value * 1000).toLocaleString(locale === "zh" ? "zh-CN" : "en");
}
