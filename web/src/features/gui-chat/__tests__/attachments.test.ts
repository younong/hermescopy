import { beforeEach, describe, expect, it, vi } from "vitest";

const compressionMocks = vi.hoisted(() => ({
  imageCompression: vi.fn(),
  optimisePng: vi.fn(),
}));

vi.mock("@jsquash/oxipng", () => ({ optimise: compressionMocks.optimisePng }));
vi.mock("browser-image-compression", () => ({ default: compressionMocks.imageCompression }));

import {
  attachmentKindFromFile,
  base64FromDataUrl,
  compressImageForUpload,
  formatBytes,
  IMAGE_ATTACHMENT_MAX_BYTES,
  IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES,
  PDF_ATTACHMENT_MAX_BYTES,
  FILE_ATTACHMENT_MAX_BYTES,
  validateComposerAttachment,
} from "../attachments";

function file(name: string, type: string, size = 1): File {
  return new File([new Uint8Array(size)], name, { type });
}

beforeEach(() => {
  compressionMocks.imageCompression.mockReset();
  compressionMocks.optimisePng.mockReset();
});

describe("gui chat attachment helpers", () => {
  it("extracts base64 payloads from data URLs", () => {
    expect(base64FromDataUrl("data:image/png;base64,abc123")).toBe("abc123");
  });

  it("detects image and PDF attachments", () => {
    expect(attachmentKindFromFile(file("cat.png", "image/png"))).toBe("image");
    expect(attachmentKindFromFile(file("brief.pdf", "application/pdf"))).toBe("pdf");
    expect(attachmentKindFromFile(file("brief.pdf", ""))).toBe("pdf");
    expect(attachmentKindFromFile(file("notes.txt", "text/plain"))).toBe("file");
  });

  it("falls back to supported image extensions for missing or generic MIME types", () => {
    expect(attachmentKindFromFile(file("cat.png", ""))).toBe("image");
    expect(attachmentKindFromFile(file("photo.JPEG", "application/octet-stream"))).toBe("image");
    expect(attachmentKindFromFile(file("vector.svg", "application/octet-stream"))).toBe("file");
    expect(attachmentKindFromFile(file("cat.png", "text/plain"))).toBe("file");
  });

  it("does not process images at or below 2MB", async () => {
    const original = file("cat.png", "image/png", IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES);

    await expect(compressImageForUpload(original)).resolves.toBe(original);
    expect(compressionMocks.optimisePng).not.toHaveBeenCalled();
    expect(compressionMocks.imageCompression).not.toHaveBeenCalled();
  });

  it("uses lossless PNG optimisation when it reaches the upload target", async () => {
    const original = file("diagram.png", "image/png", IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES + 1);
    compressionMocks.optimisePng.mockResolvedValue(
      new Uint8Array(IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES).buffer,
    );

    const compressed = await compressImageForUpload(original);

    expect(compressionMocks.optimisePng).toHaveBeenCalledWith(expect.any(ArrayBuffer), {
      level: 4,
      optimiseAlpha: false,
    });
    expect(compressed.name).toBe("diagram.png");
    expect(compressed.type).toBe("image/png");
    expect(compressed.size).toBe(IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES);
    expect(compressionMocks.imageCompression).not.toHaveBeenCalled();
  });

  it("uses third-party lossy compression when lossless optimisation fails", async () => {
    const original = file("diagram.png", "image/png", IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES + 1);
    const lossy = new File([new Uint8Array(IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES)], "diagram.jpg", {
      type: "image/jpeg",
    });
    compressionMocks.optimisePng.mockRejectedValue(new Error("WASM failed to load"));
    compressionMocks.imageCompression.mockResolvedValue(lossy);

    await expect(compressImageForUpload(original)).resolves.toBe(lossy);
    expect(compressionMocks.imageCompression).toHaveBeenCalledWith(original, {
      fileType: "image/jpeg",
      maxIteration: 20,
      maxSizeMB: 2,
      maxWidthOrHeight: 4096,
      useWebWorker: true,
    });
  });

  it("uses third-party lossy compression when lossless optimisation misses the target", async () => {
    const original = file("diagram.png", "image/png", IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES + 1);
    compressionMocks.optimisePng.mockResolvedValue(
      new Uint8Array(IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES + 1).buffer,
    );
    compressionMocks.imageCompression.mockResolvedValue(
      new File([new Uint8Array(IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES)], "diagram.jpg", {
        type: "image/jpeg",
      }),
    );

    const compressed = await compressImageForUpload(original);

    expect(compressionMocks.imageCompression).toHaveBeenCalledWith(original, {
      fileType: "image/jpeg",
      maxIteration: 20,
      maxSizeMB: 2,
      maxWidthOrHeight: 4096,
      useWebWorker: true,
    });
    expect(compressed.name).toBe("diagram.jpg");
    expect(compressed.type).toBe("image/jpeg");
    expect(compressed.size).toBe(IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES);
  });

  it("uses the lossy result without rejecting the upload", async () => {
    const original = file("photo.jpg", "image/jpeg", IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES + 1);
    const lossy = new File([new Uint8Array(IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES)], "photo.jpg", {
      type: "image/jpeg",
    });
    compressionMocks.imageCompression.mockResolvedValue(lossy);

    await expect(compressImageForUpload(original)).resolves.toBe(lossy);
    expect(compressionMocks.optimisePng).not.toHaveBeenCalled();
    expect(compressionMocks.imageCompression).toHaveBeenCalledOnce();
  });

  it("validates supported file sizes with typed failure reasons", () => {
    expect(validateComposerAttachment(file("cat.png", "image/png", IMAGE_ATTACHMENT_MAX_BYTES))).toEqual({
      kind: "image",
      ok: true,
    });
    expect(validateComposerAttachment(file("cat.png", "image/png", IMAGE_ATTACHMENT_MAX_BYTES + 1))).toEqual({
      ok: false,
      reason: "image_too_large",
    });
    expect(validateComposerAttachment(file("brief.pdf", "application/pdf", PDF_ATTACHMENT_MAX_BYTES))).toEqual({
      kind: "pdf",
      ok: true,
    });
    expect(validateComposerAttachment(file("brief.pdf", "application/pdf", PDF_ATTACHMENT_MAX_BYTES + 1))).toEqual({
      ok: false,
      reason: "pdf_too_large",
    });
    expect(validateComposerAttachment(file("data.csv", "text/csv", FILE_ATTACHMENT_MAX_BYTES))).toEqual({
      kind: "file",
      ok: true,
    });
    expect(validateComposerAttachment(file("data.csv", "text/csv", FILE_ATTACHMENT_MAX_BYTES + 1))).toEqual({
      ok: false,
      reason: "file_too_large",
    });
  });

  it("formats byte sizes for attachment cards", () => {
    expect(formatBytes(46 * 1024)).toBe("46KB");
    expect(formatBytes(2 * 1024 * 1024)).toBe("2MB");
    expect(formatBytes(1536)).toBe("1.5KB");
  });
});
