import type { GuiComposerAttachmentKind } from "./types";

export const COMPOSER_ATTACHMENT_MAX_COUNT = 10;
export const IMAGE_ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024;
export const PDF_ATTACHMENT_MAX_BYTES = 50 * 1024 * 1024;
export const FILE_ATTACHMENT_MAX_BYTES = 50 * 1024 * 1024;

const IMAGE_ATTACHMENT_EXTENSIONS = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"];

export function base64FromDataUrl(dataUrl: string): string {
  const comma = dataUrl.indexOf(",");
  return comma >= 0 ? dataUrl.slice(comma + 1) : "";
}

export function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error ?? new Error(`Could not read ${file.name}`));
    reader.onload = () => {
      if (typeof reader.result === "string") {
        resolve(reader.result);
        return;
      }
      reject(new Error(`Could not read ${file.name}`));
    };
    reader.readAsDataURL(file);
  });
}

export function attachmentKindFromFile(file: File): GuiComposerAttachmentKind | null {
  const mimeType = file.type.toLowerCase();
  const name = file.name.toLowerCase();

  if (mimeType.startsWith("image/")) return "image";
  if (mimeType === "application/pdf" || name.endsWith(".pdf")) return "pdf";
  if (
    (!mimeType || mimeType === "application/octet-stream") &&
    IMAGE_ATTACHMENT_EXTENSIONS.some((extension) => name.endsWith(extension))
  ) {
    return "image";
  }
  return "file";
}

export function validateComposerAttachment(file: File):
  | { ok: true; kind: GuiComposerAttachmentKind }
  | { ok: false; message: string } {
  const kind = attachmentKindFromFile(file);
  if (!kind) {
    return { ok: false, message: `${file.name} 暂不支持。` };
  }

  if (kind === "image" && file.size > IMAGE_ATTACHMENT_MAX_BYTES) {
    return { ok: false, message: `${file.name} 超过 10MB，无法上传。` };
  }

  if (kind === "pdf" && file.size > PDF_ATTACHMENT_MAX_BYTES) {
    return { ok: false, message: `${file.name} 超过 50MB，无法上传。` };
  }

  if (kind === "file" && file.size > FILE_ATTACHMENT_MAX_BYTES) {
    return { ok: false, message: `${file.name} 超过 50MB，无法上传。` };
  }

  return { ok: true, kind };
}

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0B";

  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unitIndex = 0;

  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }

  if (unitIndex === 0) return `${Math.round(value)}${units[unitIndex]}`;
  const rounded = value >= 10 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${rounded}${units[unitIndex]}`;
}
