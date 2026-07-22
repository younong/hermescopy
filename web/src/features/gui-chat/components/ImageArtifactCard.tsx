import { Download, ExternalLink, Image as ImageIcon, LoaderCircle } from "lucide-react";
import { useEffect, useState, type MouseEvent } from "react";
import { fetchJSON, withHermesAssetAuth } from "@/lib/api";
import { downloadSessionFile, triggerDownload } from "../files";
import type { ImageArtifactState } from "../types";

export function ImageArtifactCard({
  artifact,
  variant = "card",
}: {
  artifact: ImageArtifactState;
  variant?: "bubble" | "card";
}) {
  const filename = filenameForArtifact(artifact);
  const [downloading, setDownloading] = useState(false);
  const [downloadError, setDownloadError] = useState<string | null>(null);
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

  const downloadUrl = artifact.downloadUrl ?? artifact.url;
  const download = (event: MouseEvent<HTMLAnchorElement>) => {
    event.preventDefault();
    if (downloading) return;
    setDownloading(true);
    setDownloadError(null);
    void downloadImageArtifact(downloadUrl, filename)
      .catch((error: unknown) => {
        setDownloadError(error instanceof Error ? error.message : String(error));
      })
      .finally(() => setDownloading(false));
  };

  const openUrl = displayUrl ?? artifact.url;
  const dimensions = validImageDimensions(artifact.width, artifact.height);
  const geometryStyle = dimensions
    ? { aspectRatio: `${dimensions.width} / ${dimensions.height}` }
    : undefined;

  if (variant === "bubble") {
    return (
      <div className="w-[180px] sm:w-[220px]">
        <div
          className={dimensions ? "max-h-[320px] w-full overflow-hidden rounded-3xl bg-current/[0.04] shadow-sm" : undefined}
          data-image-geometry={dimensions ? `${dimensions.width}x${dimensions.height}` : undefined}
          style={geometryStyle}
        >
          {displayUrl ? (
            <a href={openUrl} target="_blank" rel="noreferrer" className="block h-full w-full">
              <img
                alt={artifact.title ?? "Image artifact"}
                className={dimensions ? "h-full w-full object-cover" : "max-h-[320px] w-full rounded-3xl object-cover shadow-sm"}
                height={dimensions?.height}
                loading="lazy"
                src={displayUrl}
                width={dimensions?.width}
              />
            </a>
          ) : (
            <div className={dimensions ? "flex h-full w-full items-center justify-center px-4 text-center text-xs text-text-secondary" : "flex h-[220px] w-full items-center justify-center rounded-3xl bg-current/[0.04] px-4 text-center text-xs text-text-secondary"}>
              {loadError ? "Image preview failed" : "Loading image…"}
            </div>
          )}
        </div>
        <div className="mt-1 flex justify-end">
          <a
            aria-busy={downloading}
            aria-disabled={downloading}
            aria-describedby={downloadError ? `${artifact.id}-download-error` : undefined}
            aria-label={`Download ${filename}`}
            className="inline-flex h-7 items-center gap-1 px-2 text-xs text-midground hover:text-primary"
            href={downloadUrl.startsWith("/api/") ? withHermesAssetAuth(downloadUrl) : downloadUrl}
            download={filename}
            onClick={download}
          >
            {downloading ? (
              <LoaderCircle aria-hidden className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Download aria-hidden className="h-3.5 w-3.5" />
            )}
            Download
          </a>
        </div>
        {downloadError ? (
          <p
            className="mt-1 text-xs text-destructive"
            id={`${artifact.id}-download-error`}
            role="alert"
          >
            {downloadError}
          </p>
        ) : null}
      </div>
    );
  }

  return (
    <figure className="mt-3 overflow-hidden border border-current/15 bg-background-base/60">
      <a
        href={openUrl}
        target="_blank"
        rel="noreferrer"
        className={dimensions ? "block max-h-80 overflow-hidden bg-black/20" : "block bg-black/20"}
        data-image-geometry={dimensions ? `${dimensions.width}x${dimensions.height}` : undefined}
        style={geometryStyle}
      >
        {displayUrl ? (
          <img
            alt={artifact.title ?? "Image artifact"}
            className={dimensions ? "h-full w-full object-contain" : "max-h-80 w-full object-contain"}
            height={dimensions?.height}
            loading="lazy"
            src={displayUrl}
            width={dimensions?.width}
          />
        ) : (
          <div className={dimensions ? "flex h-full w-full items-center justify-center px-4 py-8 text-sm text-text-secondary" : "flex min-h-40 items-center justify-center px-4 py-8 text-sm text-text-secondary"}>
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
          aria-busy={downloading}
          aria-disabled={downloading}
          aria-describedby={downloadError ? `${artifact.id}-download-error` : undefined}
          className="inline-flex h-7 items-center gap-1 px-2 text-xs text-midground hover:text-primary"
          href={downloadUrl.startsWith("/api/") ? withHermesAssetAuth(downloadUrl) : downloadUrl}
          download={filename}
          onClick={download}
        >
          {downloading ? (
            <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Download className="h-3.5 w-3.5" />
          )}
          Download
        </a>
        {downloadError ? (
          <span
            className="w-full text-xs text-destructive"
            id={`${artifact.id}-download-error`}
            role="alert"
          >
            {downloadError}
          </span>
        ) : null}
      </figcaption>
    </figure>
  );
}

function validImageDimensions(
  width: unknown,
  height: unknown,
): { height: number; width: number } | undefined {
  if (
    typeof width !== "number" || !Number.isFinite(width) || width <= 0 ||
    typeof height !== "number" || !Number.isFinite(height) || height <= 0
  ) {
    return undefined;
  }
  return { height, width };
}

function directDisplayUrl(url: string): string | null {
  if (url.startsWith("/api/fs/read-data-url?")) return null;
  if (url.startsWith("/api/")) return withHermesAssetAuth(url);
  return url;
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
  if (url.startsWith("data:") || url.startsWith("blob:")) {
    triggerDownload(url, filename);
    return;
  }

  if (url.startsWith("/api/fs/read-data-url?")) {
    const result = await fetchJSON<{ dataUrl?: string }>(url);
    if (!result.dataUrl) throw new Error("Image download failed: missing data URL");
    triggerDownload(result.dataUrl, filename);
    return;
  }

  if (!url.startsWith("/api/")) {
    triggerDownload(url, filename);
    return;
  }

  await downloadSessionFile(url, filename);
}
