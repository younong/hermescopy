import { React, X, createPortal, useEffect, useI18n } from "../runtime";

export function GuiChatWorkspaceDialog({ busy, children, description, onClose, title, wide = false }: {
  busy: boolean; children: any; description: string; onClose(): void; title: string; wide?: boolean;
}) {
  const { t } = useI18n();
  const titleId = `kanban-dialog-${String(title).replace(/\W+/g, "-").toLowerCase()}`;
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape" && !busy) onClose(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [busy, onClose]);
  return createPortal(
    <div className="gui-chat-workspace-dialog-backdrop" data-gui-chat role="presentation">
      <div aria-labelledby={titleId} aria-modal="true" className={`gui-chat-workspace-dialog${wide ? " is-wide" : ""}`} role="dialog">
        <button aria-label={t.common.close || "Close"} className="gui-chat-workspace-dialog-close" disabled={busy} onClick={onClose} type="button"><X aria-hidden /></button>
        <h2 id={titleId}>{title}</h2><p>{description}</p>{children}
      </div>
    </div>, document.body,
  );
}
