import { React, useState } from "../runtime";
import { GuiChatWorkspaceDialog } from "./GuiChatWorkspaceDialog";
import { kanbanTranslations, useI18n } from "../runtime";
import type { KanbanCreateTaskInput, KanbanTask, KanbanUpdateTaskInput } from "../types";

export function KanbanTaskEditor({
  assignees,
  busy,
  initialStatus,
  onClose,
  onSave,
  task,
}: {
  assignees: string[];
  busy: boolean;
  initialStatus: string;
  onClose(): void;
  onSave(input: KanbanCreateTaskInput | KanbanUpdateTaskInput): void;
  task?: KanbanTask;
}) {
  const { t } = useI18n();
  const k = kanbanTranslations(t);
  const [title, setTitle] = useState(task?.title ?? "");
  const [body, setBody] = useState(task?.body ?? "");
  const [assignee, setAssignee] = useState(task?.assignee ?? "");
  const [priority, setPriority] = useState(task?.priority ?? 0);
  const [tenant, setTenant] = useState(task?.tenant ?? "");
  const [workspacePath, setWorkspacePath] = useState(task?.workspace_path ?? "");
  const [skills, setSkills] = useState((task?.skills ?? []).join(", "));
  const create = !task;

  const save = () => {
    const common = {
      assignee: assignee || null,
      body,
      priority,
      title: title.trim(),
    };
    if (create) {
      onSave({
        ...common,
        tenant: tenant || null,
        triage: initialStatus === "triage",
        workspace_path: workspacePath || null,
        skills: skills.split(",").map((skill) => skill.trim()).filter(Boolean),
      });
    } else {
      onSave(common);
    }
  };

  return (
    <GuiChatWorkspaceDialog
      busy={busy}
      description={create ? k.taskEditorDescription : k.editTaskDescription}
      onClose={onClose}
      title={create ? k.createTaskTitle : k.editTaskTitle}
      wide
    >
      <div className="gui-chat-kanban-form-grid">
        <label className="is-wide">
          <span>{k.taskTitle}</span>
          <input autoFocus onChange={(event) => setTitle(event.target.value)} value={title} />
        </label>
        <label className="is-wide">
          <span>{k.description}</span>
          <textarea onChange={(event) => setBody(event.target.value)} value={body} />
        </label>
        <label>
          <span>{k.assignee}</span>
          <input list="kanban-assignees" onChange={(event) => setAssignee(event.target.value)} value={assignee} />
          <datalist id="kanban-assignees">{assignees.map((name) => <option key={name} value={name} />)}</datalist>
        </label>
        <label>
          <span>{k.priority}</span>
          <input onChange={(event) => setPriority(Number(event.target.value))} type="number" value={priority} />
        </label>
        {create ? <>
          <label>
            <span>{k.tenant}</span>
            <input onChange={(event) => setTenant(event.target.value)} value={tenant} />
          </label>
          <label>
            <span>{k.workspace}</span>
            <input onChange={(event) => setWorkspacePath(event.target.value)} value={workspacePath} />
          </label>
          <label className="is-wide">
            <span>{k.skills}</span>
            <input onChange={(event) => setSkills(event.target.value)} placeholder={k.skillsPlaceholder} value={skills} />
          </label>
        </> : null}
      </div>
      <div className="gui-chat-workspace-dialog-actions">
        <button disabled={busy} onClick={onClose} type="button">{t.common.cancel}</button>
        <button className="is-primary" disabled={busy || !title.trim()} onClick={save} type="button">{k.save}</button>
      </div>
    </GuiChatWorkspaceDialog>
  );
}
