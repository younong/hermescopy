import { AlertTriangle, CheckSquare2, CirclePlus, GripVertical, MessageSquareText, Network } from "lucide-react";
import { useI18n } from "@/i18n";
import type { KanbanColumn, KanbanTask } from "../types";
import { allTasks, MOVABLE_STATUSES, statusHelp, statusLabel, taskMatches } from "./kanbanUi";

export function KanbanBoardView({
  assignee,
  columns,
  laneByProfile,
  mobileStatus,
  onCreate,
  onMove,
  onOpen,
  onSelect,
  query,
  selected,
}: {
  assignee: string;
  columns: KanbanColumn[];
  laneByProfile: boolean;
  mobileStatus: string;
  onCreate(status: string): void;
  onMove(task: KanbanTask, status: string): void;
  onOpen(taskId: string): void;
  onSelect(taskId: string, checked: boolean): void;
  query: string;
  selected: Set<string>;
}) {
  const { t } = useI18n();
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const tasksById = new Map(allTasks(columns).map((task) => [task.id, task]));
  return (
    <div className="gui-chat-kanban-columns">
      {columns.map((column) => {
        const tasks = column.tasks.filter((task) => taskMatches(task, normalizedQuery, assignee));
        return (
          <section
            aria-label={statusLabel(t, column.name)}
            className={`gui-chat-kanban-column${mobileStatus === column.name ? " is-mobile-active" : ""}`}
            data-status={column.name}
            key={column.name}
            onDragOver={(event) => event.preventDefault()}
            onDrop={(event) => {
              const id = event.dataTransfer.getData("text/kanban-task");
              const task = tasksById.get(id);
              if (task) onMove(task, column.name);
            }}
          >
            <header>
              <div><h2>{statusLabel(t, column.name)}</h2><span>{tasks.length}</span></div>
              <button aria-label={t.kanban.createTask} disabled={column.name === "running" || column.name === "review"} onClick={() => onCreate(column.name)} type="button"><CirclePlus /></button>
              <p>{statusHelp(t, column.name)}</p>
            </header>
            <div className="gui-chat-kanban-cards">
              {laneByProfile ? groupByAssignee(tasks).map(([profile, laneTasks]) => (
                <div className="gui-chat-kanban-lane" key={profile}>
                  <h3>{profile || t.kanban.unassigned} <span>{laneTasks.length}</span></h3>
                  {laneTasks.map((task) => <KanbanTaskCard key={task.id} {...{ onMove, onOpen, onSelect, selected, task }} />)}
                </div>
              )) : tasks.map((task) => <KanbanTaskCard key={task.id} {...{ onMove, onOpen, onSelect, selected, task }} />)}
              {tasks.length === 0 ? <div className="gui-chat-kanban-empty">{t.kanban.noTasks}</div> : null}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function KanbanTaskCard({
  onMove,
  onOpen,
  onSelect,
  selected,
  task,
}: {
  onMove(task: KanbanTask, status: string): void;
  onOpen(taskId: string): void;
  onSelect(taskId: string, checked: boolean): void;
  selected: Set<string>;
  task: KanbanTask;
}) {
  const { t } = useI18n();
  return (
    <article
      className={`gui-chat-kanban-card${selected.has(task.id) ? " is-selected" : ""}`}
      draggable
      onDragStart={(event) => event.dataTransfer.setData("text/kanban-task", task.id)}
    >
      <div className="gui-chat-kanban-card-topline">
        <label title={t.kanban.selectForBulk}>
          <input checked={selected.has(task.id)} onChange={(event) => onSelect(task.id, event.target.checked)} type="checkbox" />
          <CheckSquare2 aria-hidden />
        </label>
        <button className="gui-chat-kanban-card-title" onClick={() => onOpen(task.id)} type="button">{task.title || t.kanban.untitled}</button>
        <GripVertical aria-hidden className="gui-chat-kanban-grip" />
      </div>
      {task.latest_summary || task.body ? <p>{task.latest_summary || task.body}</p> : null}
      <div className="gui-chat-kanban-card-meta">
        <code>{task.id}</code>
        {task.assignee ? <span>{task.assignee}</span> : <span className="is-warning">{t.kanban.unassigned}</span>}
        {task.priority ? <span>P{task.priority}</span> : null}
        {task.progress ? <span><Network /> {task.progress.done}/{task.progress.total}</span> : null}
        {task.comment_count ? <span><MessageSquareText /> {task.comment_count}</span> : null}
        {task.warnings?.count ? <span className="is-danger"><AlertTriangle /> {task.warnings.count}</span> : null}
      </div>
      <label className="gui-chat-kanban-status-select">
        <span className="sr-only">{t.kanban.status}</span>
        <select
          aria-label={`${t.kanban.status}: ${task.title}`}
          onChange={(event) => onMove(task, event.target.value)}
          value={task.status}
        >
          {MOVABLE_STATUSES.map((status) => <option key={status} value={status}>{statusLabel(t, status)}</option>)}
          {task.status === "running" ? <option value="running">{statusLabel(t, "running")}</option> : null}
        </select>
      </label>
    </article>
  );
}

function groupByAssignee(tasks: KanbanTask[]): Array<[string, KanbanTask[]]> {
  const groups = new Map<string, KanbanTask[]>();
  for (const task of tasks) {
    const key = task.assignee ?? "";
    const group = groups.get(key);
    if (group) group.push(task);
    else groups.set(key, [task]);
  }
  return [...groups.entries()].sort(([left], [right]) => left.localeCompare(right));
}
