import { authedFetch } from "@/lib/api";

export type SessionFileType =
  | "archive"
  | "audio"
  | "code"
  | "document"
  | "generic"
  | "html"
  | "image"
  | "json"
  | "pdf"
  | "presentation"
  | "spreadsheet"
  | "text"
  | "video";

export function buildSessionFileDownloadUrl(
  path: string,
  cwd?: string,
  filename?: string,
): string {
  const params = new URLSearchParams({ path });
  if (cwd) params.set("cwd", cwd);
  if (filename) params.set("filename", filename);
  return `/api/files/download?${params.toString()}`;
}

export function sessionFileType(name: string, mimeType?: string): SessionFileType {
  const mime = (mimeType ?? "").toLowerCase().split(";", 1)[0];
  const extension = name.split(/[?#]/, 1)[0]?.match(/\.([^.\/\\]+)$/)?.[1]?.toLowerCase() ?? "";

  if (mime === "text/html" || extension === "html" || extension === "htm") return "html";
  if (mime === "application/pdf" || extension === "pdf") return "pdf";
  if (mime.startsWith("image/")) return "image";
  if (mime.startsWith("audio/")) return "audio";
  if (mime.startsWith("video/")) return "video";
  if (mime.includes("spreadsheet") || mime.includes("excel") || ["csv", "xls", "xlsx"].includes(extension)) return "spreadsheet";
  if (mime.includes("presentation") || mime.includes("powerpoint") || ["ppt", "pptx"].includes(extension)) return "presentation";
  if (mime.includes("wordprocessing") || ["doc", "docx", "odt", "rtf"].includes(extension)) return "document";
  if (mime.includes("zip") || mime.includes("tar") || mime.includes("gzip") || ["7z", "bz2", "gz", "rar", "tar", "tgz", "zip"].includes(extension)) return "archive";
  if (mime.includes("json") || ["json", "jsonl"].includes(extension)) return "json";
  if (mime.startsWith("text/") || ["log", "md", "markdown", "txt"].includes(extension)) return "text";
  if (["c", "cc", "cpp", "css", "go", "java", "js", "jsx", "py", "rb", "rs", "sh", "sql", "ts", "tsx", "xml", "yaml", "yml"].includes(extension)) return "code";
  if (["aac", "flac", "m4a", "mp3", "ogg", "opus", "wav"].includes(extension)) return "audio";
  if (["avi", "mkv", "mov", "mp4", "webm"].includes(extension)) return "video";
  if (["avif", "bmp", "gif", "ico", "jpeg", "jpg", "png", "svg", "webp"].includes(extension)) return "image";
  return "generic";
}

export async function downloadSessionFile(url: string, filename: string): Promise<void> {
  if (url.startsWith("data:") || url.startsWith("blob:")) {
    triggerDownload(url, filename);
    return;
  }

  const response = url.startsWith("/api/")
    ? await authedFetch(url)
    : await fetch(url, { credentials: "include" });
  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.clone().json() as { detail?: string; error?: string };
      detail = String(body.detail ?? body.error ?? "").trim();
    } catch {
      detail = (await response.text().catch(() => "")).trim();
    }
    throw new Error(
      detail
        ? `Download failed (${response.status}): ${detail}`
        : `Download failed (${response.status})`,
    );
  }
  const blob = await response.blob();
  const objectUrl = URL.createObjectURL(blob);
  try {
    triggerDownload(objectUrl, filename);
  } finally {
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
  }
}

export function triggerDownload(url: string, filename: string): void {
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noreferrer";
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  link.remove();
}
