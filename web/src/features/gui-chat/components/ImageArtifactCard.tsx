import { Download, ExternalLink, Image as ImageIcon } from "lucide-react";
import { useEffect, useState, type MouseEvent } from "react";
import { fetchJSON } from "@/lib/api";
import type { ImageArtifactState } from "../types";

export function ImageArtifactCard({ artifact }: { artifact: ImageArtifactState }) {
  const filename = filenameForArtifact(artifact);
  const [remotePreview, setRemotePreview] = useState<{
    dataUrl: string | null;
    error: string | null;
    sourceUrl: string;
  } | null>(null);

  useEffect(() => {
    if (directDisplayUrl(artifact.url)) return;
    let cancelled = false;
    void fetchJSON<{ dataUrl?: string }>(artifact.url)
      .then((result) => {
        if (!cancelled) {
          setRemotePreview({
            dataUrl: result.dataUrl ?? null,
            error: null,
            sourceUrl: artifact.url,
          });
        }
      })
      .catch((error: Error) => {
        if (!cancelled) {
          setRemotePreview({ dataUrl: null, error: error.message, sourceUrl: artifact.url });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [artifact.url]);

  const directUrl = directDisplayUrl(artifact.url);
  const displayUrl = directUrl ?? (remotePreview?.sourceUrl === artifact.url ? remotePreview.dataUrl : null);
  const loadError = remotePreview?.sourceUrl === artifact.url ? remotePreview.error : null;

  const download = (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    void downloadImageArtifact(artifact.url, filename);
  };

  const openUrl = displayUrl ?? artifact.url;

  return (
    <figure className="mt-3 overflow-hidden border border-current/15 bg-background-base/60">
      <a href={openUrl} target="_blank" rel="noreferrer" className="block bg-black/20">
        {displayUrl ? (
          <img
            alt={artifact.title ?? "Image artifact"}
            className="max-h-80 w-full object-contain"
            loading="lazy"
            src={displayUrl}
          />
        ) : (
          <div className="flex min-h-40 items-center justify-center px-4 py-8 text-sm text-text-secondary">
            {loadError ? `Image preview failed: ${loadError}` : "Loading image preview…"}
          </div>
        )}
      </a>
      <figcaption className="flex flex-wrap items-center gap-2 border-t border-current/10 px-3 py-2 text-xs text-text-secondary">
        <ImageIcon className="h-3.5 w-3.5" />
        <span className="min-w-0 flex-1 truncate">
          {artifact.title ?? artifact.mimeType ?? "Image artifact"}
          {artifact.width && artifact.height ? ` · ${artifact.width}×${artifact.height}` : ""}
        </span>
        <a
          className="inline-flex h-7 items-center gap-1 px-2 text-xs text-midground hover:text-primary"
          href={openUrl}
          target="_blank"
          rel="noreferrer"
        >
          <ExternalLink className="h-3.5 w-3.5" />
          Open
        </a>
        <a
          className="inline-flex h-7 items-center gap-1 px-2 text-xs text-midground hover:text-primary"
          href={artifact.url}
          download={filename}
          onClick={download}
        >
          <Download className="h-3.5 w-3.5" />
          Download
        </a>
      </figcaption>
    </figure>
  );
}

function directDisplayUrl(url: string): string | null {
  return url.startsWith("/api/fs/read-data-url?") ? null : url;
}

function filenameForArtifact(artifact: ImageArtifactState): string {
  const base = (artifact.title || artifact.id || "hermes-image")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "") || "hermes-image";
  const ext = extensionForMimeType(artifact.mimeType) || extensionFromUrl(artifact.url) || "png";
  return base.endsWith(`.${ext}`) ? base : `${base}.${ext}`;
}

function extensionForMimeType(mimeType?: string): string | null {
  switch ((mimeType ?? "").toLowerCase()) {
    case "image/gif":
      return "gif";
    case "image/jpeg":
    case "image/jpg":
      return "jpg";
    case "image/png":
      return "png";
    case "image/svg+xml":
      return "svg";
    case "image/webp":
      return "webp";
    default:
      return null;
  }
}

function extensionFromUrl(url: string): string | null {
  try {
    const parsed = new URL(url, window.location.href);
    const match = parsed.pathname.match(/\.([a-z0-9]{2,5})$/i);
    return match?.[1]?.toLowerCase() ?? null;
  } catch {
    return null;
  }
}

async function downloadImageArtifact(url: string, filename: string): Promise<void> {
  const direct = () => triggerDownload(url, filename);
  if (url.startsWith("data:") || url.startsWith("blob:")) {
    direct();
    return;
  }

  try {
    if (url.startsWith("/api/fs/read-data-url?")) {
      const result = await fetchJSON<{ dataUrl?: string }>(url);
      if (!result.dataUrl) throw new Error("missing data URL");
      triggerDownload(result.dataUrl, filename);
      return;
    }

    const response = await fetch(url, { credentials: "include" });
    if (!response.ok) throw new Error(`download failed: ${response.status}`);
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    try {
      triggerDownload(objectUrl, filename);
    } finally {
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    }
  } catch {
    direct();
  }
}

function triggerDownload(url: string, filename: string): void {
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noreferrer";
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  link.remove();
}
