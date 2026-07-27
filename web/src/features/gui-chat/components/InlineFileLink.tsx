import { LoaderCircle } from "lucide-react";
import { useState, type MouseEvent, type ReactNode } from "react";

import { withHermesAssetAuth } from "@/lib/api";
import { downloadSessionFile } from "../files";
import type { FileArtifactState } from "../types";
import { FileTypeIcon } from "./FileTypeIcon";

export function InlineFileLink({
  artifact,
  children,
}: {
  artifact: FileArtifactState;
  children: ReactNode;
}) {
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const errorId = `${artifact.id}-inline-download-error`;
  const download = (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    if (downloading) return;
    setDownloading(true);
    setDownloadError(null);
    void downloadSessionFile(artifact.downloadUrl, artifact.name)
      .catch((error: unknown) => {
        setDownloadError(error instanceof Error ? error.message : String(error));
      })
      .finally(() => setDownloading(false));
  };

  return (
    <>
      <a
        aria-busy={downloading}
        aria-disabled={downloading}
        aria-describedby={downloadError ? errorId : undefined}
        aria-label={`Download ${artifact.name}`}
        className="inline-flex max-w-full items-baseline gap-1 break-words text-primary underline decoration-primary/30 underline-offset-2 transition-colors [overflow-wrap:anywhere] hover:decoration-primary/60"
        href={withHermesAssetAuth(artifact.downloadUrl)}
        onClick={download}
      >
        {downloading ? (
          <LoaderCircle aria-hidden className="relative top-0.5 h-3.5 w-3.5 shrink-0 animate-spin" />
        ) : (
          <FileTypeIcon
            className="relative top-0.5 h-3.5 w-3.5 shrink-0"
            mimeType={artifact.mimeType}
            name={artifact.name}
          />
        )}
        <span>{children}</span>
      </a>
      {downloadError ? (
        <span className="ml-1 text-xs text-destructive" id={errorId} role="alert">
          {downloadError}
        </span>
      ) : null}
    </>
  );
}
