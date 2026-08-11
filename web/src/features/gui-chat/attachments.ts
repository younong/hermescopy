import type { GuiComposerAttachmentKind } from "./types";

export const COMPOSER_ATTACHMENT_MAX_COUNT = 10;
export const IMAGE_ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024;
export const IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES = 2 * 1024 * 1024;
export const PDF_ATTACHMENT_MAX_BYTES = 50 * 1024 * 1024;
export const FILE_ATTACHMENT_MAX_BYTES = 50 * 1024 * 1024;

const IMAGE_ATTACHMENT_EXTENSIONS = [".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"];
const IMAGE_COMPRESSION_MAX_DIMENSION = 4096;
const IMAGE_COMPRESSION_MIN_QUALITY = 0.26;
const IMAGE_COMPRESSION_MAX_QUALITY = 0.82;
const IMAGE_COMPRESSION_QUALITY_ATTEMPTS = 5;
const IMAGE_COMPRESSION_MAX_RESIZE_ATTEMPTS = 10;

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

export async function compressImageForUpload(file: File): Promise<File> {
  if (file.size <= IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES) return file;

  const image = await createImageBitmap(file);
  try {
    const initialScale = Math.min(
      1,
      IMAGE_COMPRESSION_MAX_DIMENSION / Math.max(image.width, image.height),
    );
    let width = Math.max(1, Math.round(image.width * initialScale));
    let height = Math.max(1, Math.round(image.height * initialScale));
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d");
    if (!context) throw new Error(`Could not compress ${file.name}`);

    for (
      let resizeAttempt = 0;
      resizeAttempt < IMAGE_COMPRESSION_MAX_RESIZE_ATTEMPTS;
      resizeAttempt += 1
    ) {
      canvas.width = width;
      canvas.height = height;
      context.fillStyle = "#fff";
      context.fillRect(0, 0, width, height);
      context.drawImage(image, 0, 0, width, height);

      const lowestQualityBlob = await canvasToBlob(canvas, IMAGE_COMPRESSION_MIN_QUALITY);
      if (lowestQualityBlob.size <= IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES) {
        const blob = await largestFittingImageBlob(canvas, lowestQualityBlob);
        return new File([blob], jpegFilename(file.name), {
          lastModified: file.lastModified,
          type: "image/jpeg",
        });
      }

      if (resizeAttempt < IMAGE_COMPRESSION_MAX_RESIZE_ATTEMPTS - 1) {
        const scale = Math.min(
          0.8,
          Math.sqrt(IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES / lowestQualityBlob.size) * 0.95,
        );
        width = Math.max(1, Math.round(width * scale));
        height = Math.max(1, Math.round(height * scale));
      }
    }
  } finally {
    image.close();
  }

  throw new Error(
    `Could not compress ${file.name} below ${formatBytes(IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES)}`,
  );
}

async function largestFittingImageBlob(
  canvas: HTMLCanvasElement,
  fallback: Blob,
): Promise<Blob> {
  let best = fallback;
  let minimum = IMAGE_COMPRESSION_MIN_QUALITY;
  let maximum = IMAGE_COMPRESSION_MAX_QUALITY;

  for (let attempt = 0; attempt < IMAGE_COMPRESSION_QUALITY_ATTEMPTS; attempt += 1) {
    const quality = (minimum + maximum) / 2;
    const candidate = await canvasToBlob(canvas, quality);
    if (candidate.size <= IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES) {
      best = candidate;
      minimum = quality;
    } else {
      maximum = quality;
    }
  }

  return best;
}

function canvasToBlob(canvas: HTMLCanvasElement, quality: number): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => {
      if (blob) {
        resolve(blob);
        return;
      }
      reject(new Error("Could not encode image"));
    }, "image/jpeg", quality);
  });
}

function jpegFilename(name: string): string {
  const extension = name.lastIndexOf(".");
  return `${extension > 0 ? name.slice(0, extension) : name}.jpg`;
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
