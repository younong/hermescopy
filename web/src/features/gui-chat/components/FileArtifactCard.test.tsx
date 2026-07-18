// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FileArtifactCard } from "./FileArtifactCard";

afterEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("FileArtifactCard", () => {
  it("downloads a generated file when its card is clicked", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("generated-bytes", { status: 200 }),
    );
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:generated-file");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <FileArtifactCard
          artifact={{
            downloadUrl: "/api/files/download?path=%2Fworkspace%2Freport.html",
            id: "generated-report",
            kind: "file",
            mimeType: "text/html",
            name: "report.html",
            sourcePath: "/workspace/report.html",
          }}
        />,
      );
    });
    await act(async () => {
      container.querySelector("a")?.dispatchEvent(
        new MouseEvent("click", { bubbles: true, cancelable: true }),
      );
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/files/download?path=%2Fworkspace%2Freport.html",
      expect.objectContaining({ credentials: "include" }),
    );
    expect(click).toHaveBeenCalledOnce();
    expect(container.querySelector("a")?.getAttribute("aria-busy")).toBe("false");

    await act(async () => root.unmount());
  });
});
