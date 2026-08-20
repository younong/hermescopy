import { React, useEffect, useMemo, useState } from "../runtime";
import { createPortal } from "../runtime";
import { Download, Markdown, Paperclip, RefreshCw, Trash2, Upload, X, formatBytes, triggerDownload } from "../runtime";
import { kanbanTranslations, useI18n } from "../runtime";
import type { KanbanApi } from "../api";
import { kanbanErrorMessage } from "../errors";
import type {
  KanbanDiagnosticAction,
  KanbanHomeChannel,
  KanbanRunInspection,
  KanbanTask,
  KanbanTaskDetailResponse,
  KanbanWorkerLog,
} from "../types";
import { formatEpoch, statusLabel } from "./kanbanUi";

const TABS = ["overview", "activity", "runs", "diagnostics"] as const;
type DrawerTab = typeof TABS[number];
type RunAction = (operation: () => Promise<unknown>, refresh?: boolean) => Promise<void>;

interface KanbanTaskDrawerProps {
  allTasks: KanbanTask[];
  api: KanbanApi;
  board: string;
  detail: KanbanTaskDetailResponse | null;
  error: string | null;
  loading: boolean;
  onClose(): void;
  onDelete(task: KanbanTask): void;
  onEdit(task: KanbanTask): void;
  onRefresh(): Promise<void>;
}

export function KanbanTaskDrawer({
  allTasks,
  api,
  board,
  detail,
  error,
  loading,
  onClose,
  onDelete,
  onEdit,
  onRefresh,
}: KanbanTaskDrawerProps) {
  const { locale, t } = useI18n();
  const k = kanbanTranslations(t);
  const [tab, setTab] = useState<DrawerTab>("overview");
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [comment, setComment] = useState("");
  const [parentId, setParentId] = useState("");
  const [childId, setChildId] = useState("");
  const [homes, setHomes] = useState<KanbanHomeChannel[]>([]);
  const [profiles, setProfiles] = useState<string[]>([]);
  const [log, setLog] = useState<KanbanWorkerLog | null>(null);
  const [inspections, setInspections] = useState<Record<number, KanbanRunInspection>>({});
  const task = detail?.task;
  const taskId = task?.id;

  useEffect(() => {
    if (!taskId) return;
    let cancelled = false;
    void Promise.allSettled([
      api.getHomeChannels(board, taskId),
      api.listProfiles(),
    ]).then(([homeResult, profileResult]) => {
      if (cancelled) return;
      if (homeResult.status === "fulfilled") {
        setHomes(homeResult.value.home_channels);
      }
      if (profileResult.status === "fulfilled") {
        setProfiles(profileResult.value.profiles.map((profile) => profile.name));
      }
    });
    return () => { cancelled = true; };
  }, [api, board, taskId]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const runAction: RunAction = async (operation, refresh = true) => {
    setBusy(true);
    setActionError(null);
    try {
      await operation();
      if (refresh) await onRefresh();
    } catch (cause) {
      setActionError(kanbanErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  };

  const candidateTasks = useMemo(
    () => allTasks.filter((candidate) => candidate.id !== task?.id),
    [allTasks, task?.id],
  );

  return createPortal(
    <div className="gui-chat-kanban-drawer-backdrop" data-gui-chat role="presentation">
      <aside aria-label={task?.title ?? k.loadingDetail} aria-modal="true" className="gui-chat-kanban-drawer" role="dialog">
        <header>
          <div><code>{task?.id}</code><h2>{task?.title ?? k.loadingDetail}</h2></div>
          <button aria-label={t.common.close} disabled={busy} onClick={onClose} type="button"><X /></button>
        </header>
        {error || actionError ? <div className="gui-chat-kanban-inline-error" role="alert">{error || actionError}</div> : null}
        {loading && !detail ? <div className="gui-chat-kanban-loading">{k.loadingDetail}</div> : task && detail ? <>
          <nav aria-label={k.taskDetails}>
            {TABS.map((name) => (
              <button className={tab === name ? "is-active" : ""} key={name} onClick={() => setTab(name)} type="button">
                {drawerTabLabel(t, name)}
              </button>
            ))}
          </nav>
          <div className="gui-chat-kanban-drawer-body">
            {tab === "overview" ? (
              <Overview
                api={api}
                board={board}
                busy={busy}
                candidateTasks={candidateTasks}
                childId={childId}
                detail={detail}
                homes={homes}
                locale={locale}
                onDelete={onDelete}
                onEdit={onEdit}
                parentId={parentId}
                runAction={runAction}
                setChildId={setChildId}
                setHomes={setHomes}
                setParentId={setParentId}
                task={task}
              />
            ) : null}
            {tab === "activity" ? (
              <Activity
                api={api}
                board={board}
                busy={busy}
                comment={comment}
                detail={detail}
                locale={locale}
                runAction={runAction}
                setComment={setComment}
                task={task}
              />
            ) : null}
            {tab === "runs" ? (
              <Runs
                api={api}
                board={board}
                busy={busy}
                detail={detail}
                inspections={inspections}
                locale={locale}
                log={log}
                profiles={profiles}
                runAction={runAction}
                setInspections={setInspections}
                setLog={setLog}
                task={task}
              />
            ) : null}
            {tab === "diagnostics" ? (
              <Diagnostics
                api={api}
                board={board}
                busy={busy}
                detail={detail}
                onNavigate={setTab}
                runAction={runAction}
              />
            ) : null}
          </div>
        </> : null}
      </aside>
    </div>,
    document.body,
  );
}

interface OverviewProps {
  api: KanbanApi;
  board: string;
  busy: boolean;
  candidateTasks: KanbanTask[];
  childId: string;
  detail: KanbanTaskDetailResponse;
  homes: KanbanHomeChannel[];
  locale: string;
  onDelete(task: KanbanTask): void;
  onEdit(task: KanbanTask): void;
  parentId: string;
  runAction: RunAction;
  setChildId: (value: string | ((current: string) => string)) => void;
  setHomes: (value: KanbanHomeChannel[] | ((current: KanbanHomeChannel[]) => KanbanHomeChannel[])) => void;
  setParentId: (value: string | ((current: string) => string)) => void;
  task: KanbanTask;
}

function Overview({
  api,
  board,
  busy,
  candidateTasks,
  childId,
  detail,
  homes,
  locale,
  onDelete,
  onEdit,
  parentId,
  runAction,
  setChildId,
  setHomes,
  setParentId,
  task,
}: OverviewProps) {
  const { t } = useI18n();
  const k = kanbanTranslations(t);

  const automation = (kind: "specify" | "decompose") => runAction(async () => {
    const result = kind === "specify"
      ? await api.specifyTask(board, task.id)
      : await api.decomposeTask(board, task.id);
    if (!result.ok) throw new Error(result.reason ?? k.automationFailed);
  });

  const toggleHome = (home: KanbanHomeChannel) => runAction(async () => {
    if (home.subscribed) await api.unsubscribeHome(board, task.id, home.platform);
    else await api.subscribeHome(board, task.id, home.platform);
    setHomes((current) => current.map((item) => (
      item.platform === home.platform ? { ...item, subscribed: !item.subscribed } : item
    )));
  });

  const upload = (file?: File) => {
    if (file) void runAction(() => api.uploadAttachment(board, task.id, file));
  };

  const download = async (id: number, name: string) => {
    const response = await api.downloadAttachment(board, id);
    const url = URL.createObjectURL(await response.blob());
    try {
      triggerDownload(url, name);
    } finally {
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  };

  return <div className="gui-chat-kanban-detail-stack">
    <div className="gui-chat-kanban-drawer-actions">
      <button disabled={busy} onClick={() => onEdit(task)} type="button">{k.edit}</button>
      <button disabled={busy} onClick={() => void automation("specify")} type="button">{k.specify}</button>
      <button disabled={busy} onClick={() => void automation("decompose")} type="button">{k.decompose}</button>
      <button className="is-danger" disabled={busy} onClick={() => onDelete(task)} type="button"><Trash2 />{t.common.delete}</button>
    </div>
    <section>
      <h3>{k.overview}</h3>
      <dl className="gui-chat-kanban-facts">
        <div><dt>{k.status}</dt><dd>{statusLabel(t, task.status)}</dd></div>
        <div><dt>{k.assignee}</dt><dd>{task.assignee || k.unassigned}</dd></div>
        <div><dt>{k.priority}</dt><dd>{task.priority}</dd></div>
        <div><dt>{k.createdBy}</dt><dd>{task.created_by || "—"}</dd></div>
        <div><dt>{k.workspace}</dt><dd>{task.workspace_path || task.workspace_kind}</dd></div>
        <div><dt>{k.createdAt}</dt><dd>{formatEpoch(task.created_at, locale)}</dd></div>
      </dl>
    </section>
    <section>
      <h3>{k.description}</h3>
      <div className="gui-chat-kanban-markdown">{task.body ? <Markdown content={task.body} /> : k.noDescription}</div>
    </section>
    {task.result || task.latest_summary ? <section>
      <h3>{k.result}</h3>
      <div className="gui-chat-kanban-markdown"><Markdown content={task.result || task.latest_summary || ""} /></div>
    </section> : null}
    <section>
      <h3>{k.dependencies}</h3>
      <DependencyList
        api={api}
        board={board}
        busy={busy}
        childId={childId}
        detail={detail}
        parentId={parentId}
        runAction={runAction}
        setChildId={setChildId}
        setParentId={setParentId}
        tasks={candidateTasks}
      />
    </section>
    <section>
      <h3>{k.attachments}</h3>
      <label className="gui-chat-kanban-upload">
        <Upload />{k.uploadAttachment}
        <input disabled={busy} onChange={(event) => upload(event.target.files?.[0])} type="file" />
      </label>
      <div className="gui-chat-kanban-attachments">
        {detail.attachments.map((attachment) => <div key={attachment.id}>
          <Paperclip />
          <span>{attachment.filename}<small>{formatBytes(attachment.size)}</small></span>
          <button aria-label={k.downloadAttachment} disabled={busy} onClick={() => void runAction(() => download(attachment.id, attachment.filename), false)} type="button"><Download /></button>
          <button aria-label={k.deleteAttachment} disabled={busy} onClick={() => void runAction(() => api.deleteAttachment(board, attachment.id))} type="button"><Trash2 /></button>
        </div>)}
      </div>
    </section>
    <section>
      <h3>{k.notifyHomeChannels}</h3>
      {homes.length ? homes.map((home) => <label className="gui-chat-kanban-home" key={home.platform}>
        <input checked={home.subscribed} disabled={busy} onChange={() => void toggleHome(home)} type="checkbox" />
        <span><strong>{home.name}</strong><small>{home.platform} · {home.chat_id}</small></span>
      </label>) : <p className="gui-chat-kanban-muted">{k.noHomeChannels}</p>}
    </section>
  </div>;
}

interface DependencyListProps {
  api: KanbanApi;
  board: string;
  busy: boolean;
  childId: string;
  detail: KanbanTaskDetailResponse;
  parentId: string;
  runAction: RunAction;
  setChildId: (value: string | ((current: string) => string)) => void;
  setParentId: (value: string | ((current: string) => string)) => void;
  tasks: KanbanTask[];
}

function DependencyList({
  api,
  board,
  busy,
  childId,
  detail,
  parentId,
  runAction,
  setChildId,
  setParentId,
  tasks,
}: DependencyListProps) {
  const { t } = useI18n();
  const k = kanbanTranslations(t);
  return <div className="gui-chat-kanban-dependencies">
    <div>
      <strong>{k.parents}</strong>
      {detail.links.parents.map((id) => <span key={id}>
        <button onClick={() => void runAction(() => api.deleteLink(board, id, detail.task.id))} type="button">×</button>{id}
      </span>)}
      <select disabled={busy} onChange={(event) => setParentId(event.target.value)} value={parentId}>
        <option value="">{k.addParent}</option>
        {tasks.map((task) => <option key={task.id} value={task.id}>{task.title}</option>)}
      </select>
      <button disabled={!parentId || busy} onClick={() => void runAction(async () => {
        await api.addLink(board, parentId, detail.task.id);
        setParentId("");
      })} type="button">+</button>
    </div>
    <div>
      <strong>{k.children}</strong>
      {detail.links.children.map((id) => <span key={id}>
        <button onClick={() => void runAction(() => api.deleteLink(board, detail.task.id, id))} type="button">×</button>{id}
      </span>)}
      <select disabled={busy} onChange={(event) => setChildId(event.target.value)} value={childId}>
        <option value="">{k.addChild}</option>
        {tasks.map((task) => <option key={task.id} value={task.id}>{task.title}</option>)}
      </select>
      <button disabled={!childId || busy} onClick={() => void runAction(async () => {
        await api.addLink(board, detail.task.id, childId);
        setChildId("");
      })} type="button">+</button>
    </div>
  </div>;
}

interface ActivityProps {
  api: KanbanApi;
  board: string;
  busy: boolean;
  comment: string;
  detail: KanbanTaskDetailResponse;
  locale: string;
  runAction: RunAction;
  setComment: (value: string | ((current: string) => string)) => void;
  task: KanbanTask;
}

function Activity({ api, board, busy, comment, detail, locale, runAction, setComment, task }: ActivityProps) {
  const { t } = useI18n();
  const k = kanbanTranslations(t);
  return <div className="gui-chat-kanban-detail-stack">
    <section>
      <h3>{k.comments}</h3>
      <form className="gui-chat-kanban-comment" onSubmit={(event) => {
        event.preventDefault();
        const body = comment.trim();
        if (body) void runAction(async () => {
          await api.addComment(board, task.id, body);
          setComment("");
        });
      }}>
        <textarea disabled={busy} onChange={(event) => setComment(event.target.value)} placeholder={k.addComment} value={comment} />
        <button disabled={busy || !comment.trim()} type="submit">{k.comment}</button>
      </form>
      {detail.comments.length ? detail.comments.map((item) => <article className="gui-chat-kanban-timeline" key={item.id}>
        <div><strong>{item.author}</strong><time>{formatEpoch(item.created_at, locale)}</time></div>
        <Markdown content={item.body} />
      </article>) : <p className="gui-chat-kanban-muted">{k.noComments}</p>}
    </section>
    <section>
      <h3>{k.events}</h3>
      {detail.events.map((item) => <article className="gui-chat-kanban-event" key={item.id}>
        <div><strong>{item.kind}</strong><time>{formatEpoch(item.created_at, locale)}</time></div>
        {item.payload ? <pre>{JSON.stringify(item.payload, null, 2)}</pre> : null}
      </article>)}
    </section>
  </div>;
}

interface RunsProps {
  api: KanbanApi;
  board: string;
  busy: boolean;
  detail: KanbanTaskDetailResponse;
  inspections: Record<number, KanbanRunInspection>;
  locale: string;
  log: KanbanWorkerLog | null;
  profiles: string[];
  runAction: RunAction;
  setInspections: (value: Record<number, KanbanRunInspection> | ((current: Record<number, KanbanRunInspection>) => Record<number, KanbanRunInspection>)) => void;
  setLog: (value: KanbanWorkerLog | null | ((current: KanbanWorkerLog | null) => KanbanWorkerLog | null)) => void;
  task: KanbanTask;
}

function Runs({
  api,
  board,
  busy,
  detail,
  inspections,
  locale,
  log,
  profiles,
  runAction,
  setInspections,
  setLog,
  task,
}: RunsProps) {
  const { t } = useI18n();
  const k = kanbanTranslations(t);
  const [reassignProfile, setReassignProfile] = useState(task.assignee ?? "");
  const loadLog = () => runAction(async () => setLog(await api.getTaskLog(board, task.id, 100_000)), false);

  return <div className="gui-chat-kanban-detail-stack">
    <section>
      <div className="gui-chat-kanban-section-heading">
        <h3>{k.runHistory}</h3>
        <button disabled={busy} onClick={() => void loadLog()} type="button"><RefreshCw />{k.workerLog}</button>
      </div>
      {detail.runs.map((run) => <article className="gui-chat-kanban-run" key={run.id}>
        <div>
          <strong>#{run.id} · {run.status}</strong>
          <span>{run.profile || k.noProfile}</span>
          <time>{formatEpoch(run.started_at, locale)} → {formatEpoch(run.ended_at, locale)}</time>
        </div>
        {run.summary ? <Markdown content={run.summary} /> : null}
        {run.error ? <pre>{run.error}</pre> : null}
        <div>
          <button disabled={busy} onClick={() => void runAction(async () => {
            const inspection = await api.inspectRun(board, run.id);
            setInspections((current) => ({ ...current, [run.id]: inspection }));
          }, false)} type="button">{k.inspectRun}</button>
          {!run.ended_at ? <button disabled={busy} onClick={() => void runAction(() => api.terminateRun(board, run.id, "Stopped from Chat GUI"))} type="button">{k.terminateRun}</button> : null}
        </div>
        {inspections[run.id] ? <pre>{JSON.stringify(inspections[run.id], null, 2)}</pre> : null}
      </article>)}
    </section>
    <section>
      <h3>{k.workerLog}</h3>
      {log ? <>
        <pre className="gui-chat-kanban-log">{log.content || k.noWorkerLog}</pre>
        {log.truncated ? <small>{k.logTruncated}{log.path}{k.logAt}</small> : null}
      </> : <button disabled={busy} onClick={() => void loadLog()} type="button">{k.loadLog}</button>}
    </section>
    <section>
      <h3>{k.recovery}</h3>
      <div className="gui-chat-kanban-drawer-actions">
        <button disabled={busy} onClick={() => void runAction(() => api.reclaimTask(board, task.id, "Reclaimed from Chat GUI"))} type="button">{k.reclaim}</button>
        <label>
          <span>{k.reassignTo}</span>
          <select disabled={busy} onChange={(event) => setReassignProfile(event.target.value)} value={reassignProfile}>
            <option value="">{k.unassigned}</option>
            {profiles.map((profile) => <option key={profile} value={profile}>{profile}</option>)}
          </select>
        </label>
        <button disabled={busy || reassignProfile === (task.assignee ?? "")} onClick={() => void runAction(() => api.reassignTask(board, task.id, {
          profile: reassignProfile || null,
          reclaim_first: true,
          reason: "Reassigned from Chat GUI",
        }))} type="button">{k.reassign}</button>
      </div>
    </section>
  </div>;
}

interface DiagnosticsProps {
  api: KanbanApi;
  board: string;
  busy: boolean;
  detail: KanbanTaskDetailResponse;
  onNavigate(tab: DrawerTab): void;
  runAction: RunAction;
}

function Diagnostics({ api, board, busy, detail, onNavigate, runAction }: DiagnosticsProps) {
  const { locale, t } = useI18n();
  const k = kanbanTranslations(t);
  const diagnostics = detail.task.diagnostics ?? [];

  const diagnosticAction = (action: KanbanDiagnosticAction) => {
    const task = detail.task;
    if (action.kind === "reclaim") {
      void runAction(() => api.reclaimTask(board, task.id, "Reclaimed from diagnostic"));
    } else if (action.kind === "unblock") {
      void runAction(() => api.updateTask(board, task.id, { status: "ready" }));
    } else if (action.kind === "comment") {
      onNavigate("activity");
    } else if (action.kind === "reassign") {
      onNavigate("runs");
    }
  };

  return <section className="gui-chat-kanban-detail-stack">
    <h3>{k.diagnostics}</h3>
    {diagnostics.length ? diagnostics.map((diagnostic, index) => <article className={`gui-chat-kanban-diagnostic is-${diagnostic.severity}`} key={`${diagnostic.kind}-${index}`}>
      <div><strong>{diagnostic.title}</strong><span>{diagnostic.severity}</span></div>
      <p>{diagnostic.detail}</p>
      <time>{formatEpoch(diagnostic.last_seen_at, locale)}</time>
      {diagnostic.actions.map((action, actionIndex) => (
        <DiagnosticAction
          action={action}
          busy={busy}
          key={`${action.kind}-${actionIndex}`}
          onAction={() => diagnosticAction(action)}
        />
      ))}
    </article>) : <p className="gui-chat-kanban-muted">{k.noDiagnostics}</p>}
  </section>;
}

function DiagnosticAction({ action, busy, onAction }: {
  action: KanbanDiagnosticAction;
  busy: boolean;
  onAction(): void;
}) {
  const { t } = useI18n();
  const k = kanbanTranslations(t);
  const command = metadataString(action.payload, "command");
  const url = metadataString(action.payload, "url");

  if (action.kind === "cli_hint" && command) {
    return <div className="gui-chat-kanban-drawer-actions">
      <code>{command}</code>
      <button disabled={busy} onClick={() => void navigator.clipboard.writeText(command)} type="button">{k.copyCommand}</button>
    </div>;
  }
  if (action.kind === "open_docs" && url) {
    return <a href={url} rel="noreferrer" target="_blank">{action.label}</a>;
  }
  if (["reclaim", "reassign", "unblock", "comment"].includes(action.kind)) {
    return <button disabled={busy} onClick={onAction} type="button">{action.label}</button>;
  }
  return <pre>{action.label}\n{JSON.stringify(action.payload, null, 2)}</pre>;
}

function metadataString(payload: Record<string, unknown>, key: string): string | null {
  const value = payload[key];
  return typeof value === "string" ? value : null;
}

function drawerTabLabel(t: ReturnType<typeof useI18n>["t"], tab: DrawerTab): string {
  const k = kanbanTranslations(t);
  if (tab === "overview") return k.overview;
  if (tab === "activity") return k.activity;
  if (tab === "runs") return k.runs;
  return k.diagnostics;
}
