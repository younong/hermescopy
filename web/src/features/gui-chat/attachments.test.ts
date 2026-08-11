import { beforeEach, describe, expect, it, vi } from "vitest";

const compressionMocks = vi.hoisted(() => ({
  fromBlob: vi.fn(),
  optimisePng: vi.fn(),
}));

vi.mock("@jsquash/oxipng", () => ({ optimise: compressionMocks.optimisePng }));
vi.mock("image-resize-compress", () => ({ fromBlob: compressionMocks.fromBlob }));

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
} from "./attachments";

function file(name: string, type: string, size = 1): File {
  return new File([new Uint8Array(size)], name, { type });
}

beforeEach(() => {
  compressionMocks.fromBlob.mockReset();
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
    expect(compressionMocks.fromBlob).not.toHaveBeenCalled();
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
    expect(compressionMocks.fromBlob).not.toHaveBeenCalled();
  });

  it("uses third-party lossy compression when lossless optimisation misses the target", async () => {
    const original = file("diagram.png", "image/png", IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES + 1);
    compressionMocks.optimisePng.mockResolvedValue(
      new Uint8Array(IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES + 1).buffer,
    );
    compressionMocks.fromBlob.mockResolvedValue(
      new Blob([new Uint8Array(IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES)], { type: "image/jpeg" }),
    );

    const compressed = await compressImageForUpload(original);

    expect(compressionMocks.fromBlob).toHaveBeenCalledWith(original, {
      backgroundColor: "#fff",
      format: "jpeg",
      maxWidthOrHeight: 4096,
      targetSize: IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES,
      worker: true,
    });
    expect(compressed.name).toBe("diagram.jpg");
    expect(compressed.type).toBe("image/jpeg");
    expect(compressed.size).toBe(IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES);
  });

  it("rejects when the third-party compressor cannot reach the target", async () => {
    const original = file("photo.jpg", "image/jpeg", IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES + 1);
    compressionMocks.fromBlob.mockResolvedValue(
      new Blob([new Uint8Array(IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES + 1)], {
        type: "image/jpeg",
      }),
    );

    await expect(compressImageForUpload(original)).rejects.toThrow(
      "Could not compress photo.jpg below 2MB",
    );
    expect(compressionMocks.optimisePng).not.toHaveBeenCalled();
    expect(compressionMocks.fromBlob).toHaveBeenCalledOnce();
  });

  it("validates supported file sizes", () => {
    expect(validateComposerAttachment(file("cat.png", "image/png", IMAGE_ATTACHMENT_MAX_BYTES)).ok).toBe(
      true,
    );
    expect(validateComposerAttachment(file("cat.png", "image/png", IMAGE_ATTACHMENT_MAX_BYTES + 1)).ok).toBe(
      false,
    );
    expect(validateComposerAttachment(file("brief.pdf", "application/pdf", PDF_ATTACHMENT_MAX_BYTES)).ok).toBe(
      true,
    );
    expect(validateComposerAttachment(file("brief.pdf", "application/pdf", PDF_ATTACHMENT_MAX_BYTES + 1)).ok).toBe(
      false,
    );
    expect(validateComposerAttachment(file("data.csv", "text/csv", FILE_ATTACHMENT_MAX_BYTES)).ok).toBe(
      true,
    );
    expect(validateComposerAttachment(file("data.csv", "text/csv", FILE_ATTACHMENT_MAX_BYTES + 1)).ok).toBe(
      false,
    );
  });

  it("formats byte sizes for attachment cards", () => {
    expect(formatBytes(46 * 1024)).toBe("46KB");
    expect(formatBytes(2 * 1024 * 1024)).toBe("2MB");
    expect(formatBytes(1536)).toBe("1.5KB");
  });
});
