import { describe, expect, it } from "vitest";

import {
  attachmentKindFromFile,
  base64FromDataUrl,
  formatBytes,
  IMAGE_ATTACHMENT_MAX_BYTES,
  PDF_ATTACHMENT_MAX_BYTES,
  FILE_ATTACHMENT_MAX_BYTES,
  validateComposerAttachment,
} from "./attachments";

function file(name: string, type: string, size = 1): File {
  return new File([new Uint8Array(size)], name, { type });
}

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
