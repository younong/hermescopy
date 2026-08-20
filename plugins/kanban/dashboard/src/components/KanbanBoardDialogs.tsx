import { React, useState } from "../runtime";
import { GuiChatWorkspaceDialog } from "./GuiChatWorkspaceDialog";
import { kanbanTranslations, useI18n } from "../runtime";
import type { KanbanBoardMetadata, KanbanCreateBoardInput, KanbanUpdateBoardInput } from "../types";

export function KanbanBoardEditor({
  board,
  busy,
  onClose,
  onSave,
}: {
  board?: KanbanBoardMetadata;
  busy: boolean;
  onClose(): void;
  onSave(input: KanbanCreateBoardInput | KanbanUpdateBoardInput): void;
}) {
  const { t } = useI18n();
  const k = kanbanTranslations(t);
  const [slug, setSlug] = useState(board?.slug ?? "");
  const [name, setName] = useState(board?.name ?? "");
  const [description, setDescription] = useState(board?.description ?? "");
  const [icon, setIcon] = useState(board?.icon ?? "");
  const [color, setColor] = useState(board?.color ?? "#3867ed");
  const [switchBoard, setSwitchBoard] = useState(true);

  return (
    <GuiChatWorkspaceDialog
      busy={busy}
      description={board ? k.editBoardDescription : k.newBoardDescription}
      onClose={onClose}
      title={board ? k.editBoard : k.newBoardTitle}
    >
      <div className="gui-chat-kanban-form-grid">
        {!board ? <label className="is-wide"><span>{k.slug}</span><input autoFocus onChange={(event) => setSlug(event.target.value)} placeholder="project-name" value={slug} /></label> : null}
        <label className="is-wide"><span>{k.displayName}</span><input autoFocus={Boolean(board)} onChange={(event) => setName(event.target.value)} value={name} /></label>
        <label className="is-wide"><span>{k.description}</span><textarea onChange={(event) => setDescription(event.target.value)} value={description} /></label>
        <label><span>{k.icon}</span><input maxLength={8} onChange={(event) => setIcon(event.target.value)} value={icon} /></label>
        <label><span>{k.color}</span><input onChange={(event) => setColor(event.target.value)} type="color" value={color || "#3867ed"} /></label>
        {!board ? <label className="gui-chat-kanban-checkbox is-wide"><input checked={switchBoard} onChange={(event) => setSwitchBoard(event.target.checked)} type="checkbox" /><span>{k.switchAfterCreate}</span></label> : null}
      </div>
      <div className="gui-chat-workspace-dialog-actions">
        <button disabled={busy} onClick={onClose} type="button">{t.common.cancel}</button>
        <button
          className="is-primary"
          disabled={busy || (!board && !slug.trim())}
          onClick={() => onSave(board
            ? { color, description, icon, name }
            : { color, description, icon, name, slug: slug.trim(), switch: switchBoard })}
          type="button"
        >{k.save}</button>
      </div>
    </GuiChatWorkspaceDialog>
  );
}

export function KanbanBoardArchiveDialog({
  board,
  busy,
  onClose,
  onConfirm,
}: {
  board: KanbanBoardMetadata;
  busy: boolean;
  onClose(): void;
  onConfirm(): void;
}) {
  const { t } = useI18n();
  const k = kanbanTranslations(t);
  return (
    <GuiChatWorkspaceDialog
      busy={busy}
      description={k.archiveBoardConfirm.replace("{name}", board.name || board.slug)}
      onClose={onClose}
      title={k.archiveBoardTitle}
    >
      <div className="gui-chat-workspace-dialog-actions">
        <button disabled={busy} onClick={onClose} type="button">{t.common.cancel}</button>
        <button className="is-destructive" disabled={busy} onClick={onConfirm} type="button">{k.archive}</button>
      </div>
    </GuiChatWorkspaceDialog>
  );
}
