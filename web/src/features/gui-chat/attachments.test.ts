import { afterEach, describe, expect, it, vi } from "vitest";

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

afterEach(() => {
  vi.unstubAllGlobals();
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
    const createImageBitmap = vi.fn();
    vi.stubGlobal("createImageBitmap", createImageBitmap);

    await expect(compressImageForUpload(original)).resolves.toBe(original);
    expect(createImageBitmap).not.toHaveBeenCalled();
  });

  it("compresses images over 2MB to JPEG before upload", async () => {
    const original = file("photo.png", "image/png", IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES + 1);
    const close = vi.fn();
    vi.stubGlobal("createImageBitmap", vi.fn(async () => ({ width: 3000, height: 2000, close })));

    const drawImage = vi.fn();
    const toBlob = vi.fn((callback: BlobCallback) => {
      callback(new Blob([new Uint8Array(IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES - 1)], {
        type: "image/jpeg",
      }));
    });
    vi.stubGlobal("document", {
      createElement: vi.fn(() => ({
        getContext: vi.fn(() => ({
          drawImage,
          fillRect: vi.fn(),
          fillStyle: "",
        })),
        height: 0,
        toBlob,
        width: 0,
      })),
    });

    const compressed = await compressImageForUpload(original);

    expect(compressed).not.toBe(original);
    expect(compressed.name).toBe("photo.jpg");
    expect(compressed.type).toBe("image/jpeg");
    expect(compressed.size).toBeLessThanOrEqual(IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES);
    expect(drawImage).toHaveBeenCalledWith(expect.anything(), 0, 0, 3000, 2000);
    expect(toBlob).toHaveBeenCalled();
    expect(close).toHaveBeenCalledOnce();
  });

  it("reduces image dimensions until the compressed file is below 2MB", async () => {
    const original = file("photo.png", "image/png", IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES + 1);
    vi.stubGlobal("createImageBitmap", vi.fn(async () => ({
      width: 5000,
      height: 2500,
      close: vi.fn(),
    })));

    let calls = 0;
    const canvas = {
      getContext: vi.fn(() => ({
        drawImage: vi.fn(),
        fillRect: vi.fn(),
        fillStyle: "",
      })),
      height: 0,
      toBlob: vi.fn((callback: BlobCallback) => {
        calls += 1;
        const size = calls === 1
          ? IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES * 2
          : IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES;
        callback(new Blob([new Uint8Array(size)], { type: "image/jpeg" }));
      }),
      width: 0,
    };
    vi.stubGlobal("document", { createElement: vi.fn(() => canvas) });

    const compressed = await compressImageForUpload(original);

    expect(compressed.size).toBe(IMAGE_ATTACHMENT_UPLOAD_TARGET_BYTES);
    expect(canvas.width).toBeLessThan(4096);
    expect(canvas.height).toBeLessThan(2048);
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
