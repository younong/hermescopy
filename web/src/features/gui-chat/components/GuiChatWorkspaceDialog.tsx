import { useEffect, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";

export function GuiChatWorkspaceDialog({
  busy,
  children,
  description,
  onClose,
  title,
  wide = false,
}: {
  busy: boolean;
  children: ReactNode;
  description: string;
  onClose(): void;
  title: string;
  wide?: boolean;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  return createPortal(
    <div className="gui-chat-workspace-dialog-backdrop" data-gui-chat role="presentation">
      <div aria-modal="true" className={`gui-chat-workspace-dialog${wide ? " is-wide" : ""}`} role="dialog">
        <button aria-label="Close" className="gui-chat-workspace-dialog-close" disabled={busy} onClick={onClose} type="button">
          <X aria-hidden />
        </button>
        <h2>{title}</h2>
        <p>{description}</p>
        {children}
      </div>
    </div>,
    document.body,
  );
}
