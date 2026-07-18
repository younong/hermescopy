import { Download } from "lucide-react";
import type { MouseEvent } from "react";

import { withHermesAssetAuth } from "@/lib/api";
import { downloadSessionFile } from "../files";
import type { FileArtifactState } from "../types";
import { FileTypeIcon } from "./FileTypeIcon";

export function FileArtifactCard({
  artifact,
  variant = "card",
}: {
  artifact: FileArtifactState;
  variant?: "bubble" | "card";
}) {
  const download = (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    void downloadSessionFile(artifact.downloadUrl, artifact.name);
  };
  return (
    <a
      aria-label={`Download ${artifact.name}`}
      className={`flex w-full max-w-[320px] items-center gap-3 border border-current/15 bg-background-base/60 px-3 py-3 text-left shadow-sm transition-colors hover:border-primary/30 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary ${variant === "card" ? "mt-3" : "rounded-2xl"}`}
      href={withHermesAssetAuth(artifact.downloadUrl)}
      onClick={download}
    >
      <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-current/[0.04] text-primary">
        <FileTypeIcon className="h-5 w-5" mimeType={artifact.mimeType} name={artifact.name} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium" title={artifact.name}>{artifact.name}</div>
        <div className="truncate text-xs text-text-tertiary">{artifact.mimeType ?? "Generated file"}</div>
      </div>
      <Download aria-hidden className="h-4 w-4 shrink-0" />
    </a>
  );
}
