// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  buildSessionFileDownloadUrl,
  downloadSessionFile,
  sessionFileType,
} from "./files";

afterEach(() => {
  vi.restoreAllMocks();
  document.body.innerHTML = "";
  delete window.__HERMES_SESSION_TOKEN__;
  delete window.__HERMES_AUTH_REQUIRED__;
  sessionStorage.clear();
});

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

  it("downloads authenticated API responses through a blob link", async () => {
    window.__HERMES_SESSION_TOKEN__ = "session-token";
    window.__HERMES_AUTH_REQUIRED__ = false;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("download-bytes", { status: 200 }),
    );
    const objectUrl = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:download");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);

    await downloadSessionFile("/api/files/download?path=report.html", "report.html");

    const [, init] = fetchMock.mock.calls[0];
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/files/download?path=report.html");
    expect(new Headers(init?.headers).get("X-Hermes-Session-Token")).toBe("session-token");
    expect(init?.credentials).toBe("include");
    expect(objectUrl).toHaveBeenCalled();
    expect(click).toHaveBeenCalledOnce();
  });

  it("reports structured API download failures", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Path not found" }), {
        headers: { "Content-Type": "application/json" },
        status: 404,
      }),
    );

    await expect(
      downloadSessionFile("/api/files/download?path=missing.txt", "missing.txt"),
    ).rejects.toThrow("Download failed (404): Path not found");
  });
});
