import { React, useEffect, useMemo, useState } from "../runtime";
import { Archive, ChevronDown, Pencil, Plus, RefreshCw, Search, Settings2, Zap } from "../runtime";
import { GuiChatWorkspaceDialog } from "./GuiChatWorkspaceDialog";
import { kanbanTranslations, useI18n } from "../runtime";
import { kanbanApi, type KanbanApi } from "../api";
import { kanbanErrorMessage } from "../errors";
import type { KanbanBoardMetadata, KanbanCreateBoardInput, KanbanCreateTaskInput, KanbanTask, KanbanUpdateBoardInput, KanbanUpdateTaskInput } from "../types";
import { useKanbanBoard } from "../useKanbanBoard";
import { KanbanBoardArchiveDialog, KanbanBoardEditor } from "./KanbanBoardDialogs";
import { KanbanBoardView } from "./KanbanBoardView";
import { KanbanSettingsDialog } from "./KanbanSettingsDialog";
import { KanbanTaskDrawer } from "./KanbanTaskDrawer";
import { KanbanTaskEditor } from "./KanbanTaskEditor";
import { KanbanTransitionDialog, type KanbanTransitionRequest } from "./KanbanTransitionDialog";
import { allTasks, statusLabel, transitionNeedsInput, transitionPayload } from "./kanbanUi";

export function GuiChatKanbanPane({ api = kanbanApi }: { api?: KanbanApi }) {
  const { t } = useI18n();
  const k = kanbanTranslations(t);
  const controller = useKanbanBoard({ api });
  const [query, setQuery] = useState("");
  const [assignee, setAssignee] = useState("");
  const [laneByProfile, setLaneByProfile] = useState(false);
  const [mobileStatus, setMobileStatus] = useState("todo");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [taskEditor, setTaskEditor] = useState<{ status: string; task?: KanbanTask } | null>(null);
  const [transition, setTransition] = useState<KanbanTransitionRequest | null>(null);
  const [boardEditor, setBoardEditor] = useState<KanbanBoardMetadata | "new" | null>(null);
  const [archiveBoard, setArchiveBoard] = useState<KanbanBoardMetadata | null>(null);
  const [deleteTask, setDeleteTask] = useState<KanbanTask | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const tasks = useMemo(() => allTasks(controller.board?.columns ?? []), [controller.board]);
  const warningTasks = useMemo(
    () => tasks.filter((task) => task.warnings?.count),
    [tasks],
  );
  const { setFilters } = controller;
  const activeBoardMeta = controller.boards.find((board) => board.slug === controller.activeBoard);

  useEffect(() => {
    let cancelled = false;
    api.getConfig().then((config) => {
      if (cancelled) return;
      setLaneByProfile(config.lane_by_profile);
      if (config.default_tenant) setFilters((current) => ({ ...current, tenant: config.default_tenant }));
      if (config.include_archived_by_default) setFilters((current) => ({ ...current, include_archived: true }));
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [api, setFilters]);

  const run = async (operation: () => Promise<unknown>, success?: string) => {
    setBusy(true);
    setFeedback(null);
    try {
      await operation();
      if (success) setFeedback(success);
    } catch (cause) {
      setFeedback(kanbanErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  };

  const requestMove = (task: KanbanTask, status: string) => {
    if (status === task.status) return;
    if (status === "running" || status === "review") {
      setFeedback(k.dispatchManagedStatus);
      return;
    }
    if (transitionNeedsInput(status)) {
      setTransition({ ids: [task.id], status });
      return;
    }
    void applyTransition({ ids: [task.id], status }, "");
  };

  const applyTransition = async (request: KanbanTransitionRequest, detail: string) => {
    await run(async () => {
      if (request.ids.length === 1) {
        await controller.runMutation((client, board) => client.updateTask(board, request.ids[0], transitionPayload(request.status, detail)));
      } else {
        const payload = transitionPayload(request.status, detail);
        const result = await controller.runMutation((client, board) => client.bulkUpdate(board, { ids: request.ids, ...payload }));
        const failed = result.results.filter((item) => !item.ok);
        if (failed.length) throw new Error(failed.map((item) => `${item.id}: ${item.error}`).join("\n"));
        setSelected(new Set());
      }
      setTransition(null);
    });
  };

  const saveTask = (input: KanbanCreateTaskInput | KanbanUpdateTaskInput) => void run(async () => {
    if (taskEditor?.task) {
      const editedTask = taskEditor.task;
      await controller.runMutation((client, board) => client.updateTask(board, editedTask.id, input as KanbanUpdateTaskInput));
    } else if (taskEditor) {
      const requestedStatus = taskEditor.status;
      const response = await controller.runMutation((client, board) => client.createTask(board, input as KanbanCreateTaskInput));
      if (response.warning) setFeedback(`${k.taskCreatedWarning}${response.warning}`);
      if (response.task && response.task.status !== requestedStatus) {
        if (requestedStatus === "running" || requestedStatus === "review") {
          setFeedback(k.dispatchManagedStatus);
        } else if (transitionNeedsInput(requestedStatus)) {
          setTransition({ ids: [response.task.id], status: requestedStatus });
        } else {
          const createdTask = response.task;
          await controller.runMutation((client, board) => client.updateTask(board, createdTask.id, transitionPayload(requestedStatus)));
        }
      }
    }
    setTaskEditor(null);
  });

  const saveBoard = (input: KanbanCreateBoardInput | KanbanUpdateBoardInput) => void run(async () => {
    if (boardEditor === "new") {
      const createInput = input as KanbanCreateBoardInput;
      const result = await api.createBoard(createInput);
      await controller.refreshBoards();
      if (createInput.switch) controller.setActiveBoard(result.board.slug);
    } else if (boardEditor) {
      await api.updateBoard(boardEditor.slug, input as KanbanUpdateBoardInput);
      await controller.refreshBoards();
    }
    setBoardEditor(null);
  });

  const archiveCurrentBoard = () => archiveBoard && void run(async () => {
    await api.removeBoard(archiveBoard.slug);
    setArchiveBoard(null);
    await controller.refreshBoards();
  });

  const deleteSelectedTask = () => deleteTask && void run(async () => {
    await controller.runMutation((client, board) => client.deleteTask(board, deleteTask.id));
    controller.setSelectedTaskId(null);
    setDeleteTask(null);
  });

  const bulk = (kind: "done" | "archive") => {
    const ids = [...selected];
    if (!ids.length) return;
    if (kind === "done") setTransition({ ids, status: "done" });
    else void run(async () => {
      const result = await controller.runMutation((client, board) => client.bulkUpdate(board, { ids, archive: true }));
      const failed = result.results.filter((item) => !item.ok);
      if (failed.length) throw new Error(failed.map((item) => `${item.id}: ${item.error}`).join("\n"));
      setSelected(new Set());
    });
  };

  const boardTitle = activeBoardMeta?.name || activeBoardMeta?.slug || controller.activeBoard;
  const error = controller.error || controller.socketError || feedback;

  return (
    <section aria-label={k.board} className="gui-chat-workspace-pane gui-chat-kanban-pane" data-kanban-pane>
      <header className="gui-chat-kanban-toolbar">
        <div className="gui-chat-kanban-board-picker">
          <span style={{ background: activeBoardMeta?.color || "#3867ed" }}>{activeBoardMeta?.icon || boardTitle.charAt(0).toUpperCase()}</span>
          <label><span className="sr-only">{k.board}</span><select onChange={(event) => { setSelected(new Set()); controller.setActiveBoard(event.target.value); }} value={controller.activeBoard}>{controller.boards.map((board) => <option key={board.slug} value={board.slug}>{board.name || board.slug}{board.slug === controller.serverCurrentBoard ? ` · ${k.current}` : ""}</option>)}</select><ChevronDown /></label>
        </div>
        <div className="gui-chat-kanban-toolbar-actions">
          <button className="gui-chat-workspace-primary-button" onClick={() => setTaskEditor({ status: mobileStatus })} type="button"><Plus />{k.newTask}</button>
          <button aria-label={k.newBoardTitle} className="gui-chat-workspace-icon-button" onClick={() => setBoardEditor("new")} type="button"><Plus /></button>
          <button aria-label={k.editBoard} className="gui-chat-workspace-icon-button" disabled={!activeBoardMeta} onClick={() => activeBoardMeta && setBoardEditor(activeBoardMeta)} type="button"><Pencil /></button>
          <button aria-label={k.archiveBoardTitle} className="gui-chat-workspace-icon-button is-destructive" disabled={!activeBoardMeta || controller.boards.length < 2} onClick={() => activeBoardMeta && setArchiveBoard(activeBoardMeta)} type="button"><Archive /></button>
          <button className="gui-chat-kanban-text-button" disabled={busy || controller.serverCurrentBoard === controller.activeBoard} onClick={() => void run(async () => { await api.switchBoard(controller.activeBoard); await controller.refreshBoards(); }, k.currentBoardUpdated)} type="button">{k.setCurrent}</button>
          <button aria-label={k.nudgeDispatcher} className="gui-chat-workspace-icon-button" disabled={busy} onClick={() => void run(() => controller.runMutation((client, board) => client.dispatch(board)), k.dispatchComplete)} type="button"><Zap /></button>
          <button aria-label={k.settings} className="gui-chat-workspace-icon-button" onClick={() => setSettingsOpen(true)} type="button"><Settings2 /></button>
          <button aria-label={t.common.refresh} className="gui-chat-workspace-icon-button" disabled={controller.loading} onClick={() => void controller.refresh()} type="button"><RefreshCw className={controller.loading ? "animate-spin" : ""} /></button>
        </div>
      </header>

      <div className="gui-chat-kanban-filters">
        <label className="gui-chat-workspace-search"><Search /><input aria-label={k.filterCards} onChange={(event) => setQuery(event.target.value)} placeholder={k.filterCards} value={query} /></label>
        <label><span>{k.tenant}</span><select onChange={(event) => setFilters((current) => ({ ...current, tenant: event.target.value || undefined }))} value={controller.filters.tenant ?? ""}><option value="">{k.allTenants}</option>{controller.board?.tenants.map((tenant) => <option key={tenant} value={tenant}>{tenant}</option>)}</select></label>
        <label><span>{k.assignee}</span><select onChange={(event) => setAssignee(event.target.value)} value={assignee}><option value="">{k.allProfiles}</option>{controller.board?.assignees.map((name) => <option key={name} value={name}>{name}</option>)}</select></label>
        <label className="gui-chat-kanban-checkbox"><input checked={Boolean(controller.filters.include_archived)} onChange={(event) => setFilters((current) => ({ ...current, include_archived: event.target.checked }))} type="checkbox" /><span>{k.showArchived}</span></label>
        <label className="gui-chat-kanban-checkbox"><input checked={laneByProfile} onChange={(event) => setLaneByProfile(event.target.checked)} type="checkbox" /><span>{k.lanesByProfile}</span></label>
        <span className={`gui-chat-kanban-connection is-${controller.connectionState}`}>{controller.connectionState}</span>
      </div>

      <div className="gui-chat-kanban-mobile-tabs">{controller.board?.columns.map((column) => <button className={mobileStatus === column.name ? "is-active" : ""} key={column.name} onClick={() => setMobileStatus(column.name)} type="button">{statusLabel(t, column.name)}<span>{column.tasks.length}</span></button>)}</div>
      {error ? <div className={`gui-chat-workspace-feedback${feedback && !controller.error ? " is-info" : ""}`} role={controller.error ? "alert" : "status"}>{error}<button onClick={() => setFeedback(null)} type="button">×</button></div> : null}
      {warningTasks.length ? <button className="gui-chat-kanban-attention" onClick={() => controller.setSelectedTaskId(warningTasks[0].id)} type="button">{warningTasks.length} {k.tasksNeedAttention}</button> : null}
      {selected.size ? <div className="gui-chat-kanban-bulk"><strong>{selected.size} {k.selected}</strong><button onClick={() => bulk("done")} type="button">{k.complete}</button><button onClick={() => bulk("archive")} type="button">{k.archive}</button><label><span>{k.assignee}</span><select onChange={(event) => { const value = event.target.value; if (value) void run(() => controller.runMutation((client, board) => client.bulkUpdate(board, { ids: [...selected], assignee: value === "__none__" ? "" : value, reclaim_first: true }))); }} value=""><option value="">{k.apply}</option><option value="__none__">{k.unassigned}</option>{controller.board?.assignees.map((name) => <option key={name} value={name}>{name}</option>)}</select></label><button onClick={() => setSelected(new Set())} type="button">{k.clear}</button></div> : null}

      {controller.loading && !controller.board ? <div className="gui-chat-kanban-loading">{k.loading}</div> : controller.board ? <KanbanBoardView assignee={assignee} columns={controller.board.columns} laneByProfile={laneByProfile} mobileStatus={mobileStatus} onCreate={(status) => setTaskEditor({ status })} onMove={requestMove} onOpen={controller.setSelectedTaskId} onSelect={(id, checked) => setSelected((current) => { const next = new Set(current); if (checked) next.add(id); else next.delete(id); return next; })} query={query} selected={selected} /> : null}

      {taskEditor ? <KanbanTaskEditor assignees={controller.board?.assignees ?? []} busy={busy} initialStatus={taskEditor.status} onClose={() => setTaskEditor(null)} onSave={saveTask} task={taskEditor.task} /> : null}
      {transition ? <KanbanTransitionDialog busy={busy} onClose={() => setTransition(null)} onConfirm={(detail) => void applyTransition(transition, detail)} request={transition} /> : null}
      {boardEditor ? <KanbanBoardEditor board={boardEditor === "new" ? undefined : boardEditor} busy={busy} onClose={() => setBoardEditor(null)} onSave={saveBoard} /> : null}
      {archiveBoard ? <KanbanBoardArchiveDialog board={archiveBoard} busy={busy} onClose={() => setArchiveBoard(null)} onConfirm={archiveCurrentBoard} /> : null}
      {settingsOpen ? <KanbanSettingsDialog api={api} onClose={() => setSettingsOpen(false)} /> : null}
      {deleteTask ? <GuiChatWorkspaceDialog busy={busy} description={k.deleteTaskConfirm.replace("{name}", deleteTask.title)} onClose={() => setDeleteTask(null)} title={k.deleteTaskTitle}><div className="gui-chat-workspace-dialog-actions"><button disabled={busy} onClick={() => setDeleteTask(null)} type="button">{t.common.cancel}</button><button className="is-destructive" disabled={busy} onClick={deleteSelectedTask} type="button">{t.common.delete}</button></div></GuiChatWorkspaceDialog> : null}
      {controller.selectedTaskId ? <KanbanTaskDrawer key={controller.selectedTaskId} allTasks={tasks} api={api} board={controller.activeBoard} detail={controller.selectedTask} error={controller.selectedTaskError} loading={controller.selectedTaskLoading} onClose={() => controller.setSelectedTaskId(null)} onDelete={setDeleteTask} onEdit={(task) => setTaskEditor({ status: task.status, task })} onRefresh={async () => { await Promise.all([controller.refresh(), controller.refreshSelectedTask()]); }} /> : null}
    </section>
  );
}
