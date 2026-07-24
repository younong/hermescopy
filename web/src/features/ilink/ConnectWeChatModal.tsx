import { useEffect, useMemo, useRef } from "react";
import { createPortal } from "react-dom";
import { X } from "lucide-react";

import { api } from "@/lib/api";
import { cn, themedBody } from "@/lib/utils";
import { ILinkEnrollmentPanel } from "./ILinkEnrollmentPanel";
import { useILinkEnrollment } from "./useILinkEnrollment";

interface Props {
  onClose: () => void;
}

function enrollmentDeviceId(): string {
  const key = "hermes-ilink-auth-enrollment-device";
  const existing = window.sessionStorage.getItem(key);
  if (existing) return existing;
  const id = crypto.randomUUID();
  window.sessionStorage.setItem(key, id);
  return id;
}

export function ConnectWeChatModal({ onClose }: Props) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const operations = useMemo(
    () => ({
      create: () => api.createILinkEnrollment(enrollmentDeviceId()),
      get: api.getILinkEnrollment,
    }),
    [],
  );
  const enrollment = useILinkEnrollment(operations);

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialogRef.current?.querySelector<HTMLButtonElement>("[data-close]")?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
      previous?.focus?.();
    };
  }, [onClose]);

  return createPortal(
    <div
      aria-labelledby="connect-wechat-title"
      aria-modal="true"
      className="fixed inset-0 z-[200] flex items-center justify-center bg-background/85 p-4"
      onClick={(event) => event.target === event.currentTarget && onClose()}
      role="dialog"
    >
      <div
        className={cn(themedBody, "relative w-full max-w-md border border-border bg-card p-6 shadow-2xl")}
        ref={dialogRef}
      >
        <button
          aria-label="Close"
          className="absolute right-3 top-3 rounded-md p-1 text-muted-foreground hover:text-foreground"
          data-close
          onClick={onClose}
          type="button"
        >
          <X className="h-4 w-4" />
        </button>
        <h2 className="mb-4 pr-8 text-lg font-semibold" id="connect-wechat-title">
          Connect WeChat
        </h2>
        <ILinkEnrollmentPanel {...enrollment} ownerLinked />
      </div>
    </div>,
    document.body,
  );
}
