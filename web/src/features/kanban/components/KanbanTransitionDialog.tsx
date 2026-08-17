import { useState } from "react";
import { GuiChatWorkspaceDialog } from "@/features/gui-chat/components/GuiChatWorkspaceDialog";
import { kanbanTranslations, useI18n } from "@/i18n";
import { statusLabel, transitionNeedsInput } from "./kanbanUi";

export interface KanbanTransitionRequest {
  ids: string[];
  status: string;
}

export function KanbanTransitionDialog({
  busy,
  onClose,
  onConfirm,
  request,
}: {
  busy: boolean;
  onClose(): void;
  onConfirm(detail: string): void;
  request: KanbanTransitionRequest;
}) {
  const { t } = useI18n();
  const k = kanbanTranslations(t);
  const [detail, setDetail] = useState("");
  const need = transitionNeedsInput(request.status);
  const target = statusLabel(t, request.status);

  return (
    <GuiChatWorkspaceDialog
      busy={busy}
      description={need === "summary"
        ? k.completionSummary.replace("{label}", target)
        : request.status === "scheduled"
          ? k.confirmScheduled ?? "Explain the known delay or follow-up."
          : k.confirmBlocked}
      onClose={onClose}
      title={`${target} · ${request.ids.length}`}
    >
      <label className="gui-chat-kanban-field">
        <span>{need === "summary" ? k.completionSummaryRequired : k.reason}</span>
        <textarea
          autoFocus
          onChange={(event) => setDetail(event.target.value)}
          placeholder={need === "summary" ? k.completionSummaryRequired : k.reasonPlaceholder}
          value={detail}
        />
      </label>
      <div className="gui-chat-workspace-dialog-actions">
        <button disabled={busy} onClick={onClose} type="button">{t.common.cancel}</button>
        <button
          className="is-primary"
          disabled={busy || !detail.trim()}
          onClick={() => onConfirm(detail.trim())}
          type="button"
        >
          {k.apply}
        </button>
      </div>
    </GuiChatWorkspaceDialog>
  );
}
