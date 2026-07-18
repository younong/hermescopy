// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ImageArtifactCard } from "./ImageArtifactCard";

afterEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("ImageArtifactCard", () => {
  it("keeps the preview link and shows an explicit bubble download action", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("image-bytes", { status: 200 }),
    );
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:downloaded-image");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <ImageArtifactCard
          artifact={{
            downloadUrl: "/api/files/download?path=%2Fworkspace%2Fcat.png",
            id: "history-image",
            mimeType: "image/png",
            title: "cat",
            url: "data:image/png;base64,iVBORw0KGgo=",
          }}
          variant="bubble"
        />,
      );
    });

    expect(container.querySelector('a[target="_blank"] img')).not.toBeNull();
    const download = container.querySelector('a[aria-label="Download cat.png"]');
    expect(download?.textContent).toContain("Download");

    await act(async () => {
      download?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/files/download?path=%2Fworkspace%2Fcat.png",
      expect.objectContaining({ credentials: "include" }),
    );
    expect(click).toHaveBeenCalledOnce();

    await act(async () => root.unmount());
  });

  it("shows bubble download failures", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Path not found" }), {
        headers: { "Content-Type": "application/json" },
        status: 404,
      }),
    );
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <ImageArtifactCard
          artifact={{
            downloadUrl: "/api/files/download?path=missing.png",
            id: "missing-image",
            mimeType: "image/png",
            title: "missing",
            url: "data:image/png;base64,iVBORw0KGgo=",
          }}
          variant="bubble"
        />,
      );
    });
    await act(async () => {
      container.querySelector('a[aria-label="Download missing.png"]')?.dispatchEvent(
        new MouseEvent("click", { bubbles: true, cancelable: true }),
      );
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      "Download failed (404): Path not found",
    );

    await act(async () => root.unmount());
  });
});
