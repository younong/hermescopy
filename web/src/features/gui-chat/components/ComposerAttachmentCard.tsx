import { Button } from "@nous-research/ui/ui/components/button";
import { FileText, Loader2, X } from "lucide-react";

import { formatBytes } from "../attachments";
import type { GuiComposerAttachment } from "../types";

export function ComposerAttachmentCard({
  attachment,
  disabled,
  onRemove,
}: {
  attachment: GuiComposerAttachment;
  disabled?: boolean;
  onRemove: (id: string) => void;
}) {
  const isPdf = attachment.kind === "pdf";
  const isUploading = attachment.status === "uploading";
  const isError = attachment.status === "error";
  const typeLabel = attachment.kind === "image" ? "Image" : isPdf ? "PDF" : "File";
  const meta = `${typeLabel} · ${formatBytes(attachment.sizeBytes)}`;

  return (
    <div
      className={[
        "group relative flex h-[72px] w-[240px] shrink-0 items-center gap-3 rounded-2xl border px-3 py-2 text-left shadow-sm transition",
        isError
          ? "border-destructive/40 bg-destructive/10 text-destructive"
          : "border-current/10 bg-current/[0.04] text-text-primary hover:bg-current/[0.07]",
      ].join(" ")}
    >
      <div className="flex h-12 w-12 shrink-0 items-center justify-center overflow-hidden rounded-lg bg-background-base/70">
        {attachment.kind === "image" && attachment.previewUrl ? (
          <img
            alt=""
            className="h-full w-full object-cover"
            draggable={false}
            src={attachment.previewUrl}
          />
        ) : (
          <div className="flex h-10 w-8 items-center justify-center rounded-md bg-destructive text-xs font-bold text-white">
            <FileText className="h-4 w-4" />
          </div>
        )}
      </div>

      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium leading-5" title={attachment.name}>
          {attachment.name}
        </div>
        <div className={isError ? "truncate text-xs leading-5" : "truncate text-xs leading-5 text-text-tertiary"}>
          {isUploading ? (
            <span className="inline-flex items-center gap-1">
              <Loader2 className="h-3 w-3 animate-spin" />
              处理中...
            </span>
          ) : isError ? (
            attachment.error || "上传失败"
          ) : (
            meta
          )}
        </div>
      </div>

      <Button
        type="button"
        size="icon"
        ghost
        aria-label={`Remove ${attachment.name}`}
        className="absolute -right-2 -top-2 h-6 w-6 rounded-full border border-current/10 bg-background-base opacity-0 shadow-sm transition group-hover:opacity-100 group-focus-within:opacity-100"
        disabled={disabled || isUploading || attachment.status === "uploaded"}
        onClick={() => onRemove(attachment.id)}
      >
        <X className="h-3.5 w-3.5" />
      </Button>
    </div>
  );
}
