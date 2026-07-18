import { describe, expect, it } from "vitest";

import { buildSessionFileDownloadUrl, sessionFileType } from "./files";

describe("session files", () => {
  it("builds encoded session download URLs", () => {
    expect(buildSessionFileDownloadUrl("outputs/report.html", "/tmp/my project", "report final.html")).toBe(
      "/api/files/download?path=outputs%2Freport.html&cwd=%2Ftmp%2Fmy+project&filename=report+final.html",
    );
  });

  it.each([
    ["page.html", undefined, "html"],
    ["download", "application/pdf", "pdf"],
    ["archive.zip", undefined, "archive"],
    ["data.xlsx", undefined, "spreadsheet"],
    ["movie.mp4", undefined, "video"],
    ["unknown.bin", undefined, "generic"],
  ] as const)("classifies %s as %s", (name, mime, expected) => {
    expect(sessionFileType(name, mime)).toBe(expected);
  });
});
