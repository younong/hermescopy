import { Download } from "lucide-react";
import { useEffect, useState, type MouseEvent } from "react";

import { fetchJSON, withHermesAssetAuth } from "@/lib/api";
import { formatBytes } from "../attachments";
import { downloadSessionFile } from "../files";
import type { MessageAttachmentState } from "../types";
import { FileTypeIcon } from "./FileTypeIcon";

export function MessageAttachmentCard({
  attachment,
  variant = "card",
}: {
  attachment: MessageAttachmentState;
  variant?: "bubble" | "card";
}) {
  const isPdf = attachment.kind === "pdf";
  const previewUrl = useAttachmentPreviewUrl(attachment);
  const typeLabel = attachment.kind === "image" ? "Image" : isPdf ? "PDF" : "File";
  const meta = [
    typeLabel,
    formatBytes(attachment.sizeBytes),
    isPdf && attachment.pagesAttached ? `${attachment.pagesAttached} pages` : null,
  ]
    .filter(Boolean)
    .join(" · ");
  const download = (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    if (attachment.downloadUrl) {
      void downloadSessionFile(attachment.downloadUrl, attachment.name);
    }
  };

  if (variant === "bubble" && attachment.kind === "image" && previewUrl) {
    return attachment.downloadUrl ? (
      <a
        aria-label={`Download ${attachment.name}`}
        href={withHermesAssetAuth(attachment.downloadUrl)}
        onClick={download}
      >
        <img
          alt={attachment.name}
          className="max-h-[320px] w-[180px] rounded-3xl object-cover shadow-sm sm:w-[220px]"
          draggable={false}
          src={previewUrl}
        />
      </a>
    ) : (
      <img
        alt={attachment.name}
        className="max-h-[320px] w-[180px] rounded-3xl object-cover shadow-sm sm:w-[220px]"
        draggable={false}
        src={previewUrl}
      />
    );
  }

  const content = (
    <>
      <div className="flex h-11 w-11 shrink-0 items-center justify-center overflow-hidden rounded-lg bg-current/[0.04]">
        {attachment.kind === "image" && previewUrl ? (
          <img alt="" className="h-full w-full object-cover" draggable={false} src={previewUrl} />
        ) : (
          <div className="flex h-9 w-7 items-center justify-center rounded-md bg-destructive text-white">
            <FileTypeIcon mimeType={attachment.mimeType} name={attachment.name} />
          </div>
        )}
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium leading-5" title={attachment.name}>
          {attachment.name}
        </div>
        <div className="truncate text-xs leading-5 text-text-tertiary">{meta}</div>
      </div>
      {attachment.downloadUrl ? <Download aria-hidden className="h-4 w-4 shrink-0" /> : null}
    </>
  );
  const className = "flex h-[64px] w-full max-w-[280px] items-center gap-3 rounded-2xl border border-current/10 bg-background-base/60 px-3 py-2 text-left shadow-sm sm:w-[260px]";

  return attachment.downloadUrl ? (
    <a
      aria-label={`Download ${attachment.name}`}
      className={`${className} transition-colors hover:border-primary/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary`}
      href={withHermesAssetAuth(attachment.downloadUrl)}
      onClick={download}
    >
      {content}
    </a>
  ) : (
    <div aria-disabled="true" className={className} title="Original file is unavailable">
      {content}
    </div>
  );
}

function useAttachmentPreviewUrl(attachment: MessageAttachmentState): string | undefined {
  const source = attachment.previewUrl;
  const [resolved, setResolved] = useState<{ source: string; url?: string }>();

  useEffect(() => {
    if (attachment.kind !== "image" || !source?.startsWith("/api/fs/read-data-url?")) return;
    let cancelled = false;
    void fetchJSON<{ dataUrl?: string }>(source)
      .then((result) => {
        if (!cancelled) setResolved({ source, url: result.dataUrl });
      })
      .catch(() => {
        if (!cancelled) setResolved({ source });
      });
    return () => {
      cancelled = true;
    };
  }, [attachment.kind, source]);

  if (attachment.kind !== "image" || !source) return undefined;
  if (!source.startsWith("/api/fs/read-data-url?")) return source;
  return resolved?.source === source ? resolved.url : undefined;
}
