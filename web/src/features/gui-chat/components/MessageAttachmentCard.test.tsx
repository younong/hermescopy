// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { MessageAttachmentCard } from "./MessageAttachmentCard";

afterEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("MessageAttachmentCard", () => {
  it("resolves filesystem image previews before rendering the image", async () => {
    const dataUrl = "data:image/png;base64,iVBORw0KGgo=";
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ dataUrl }), {
        headers: { "Content-Type": "application/json" },
        status: 200,
      }),
    );
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <MessageAttachmentCard
          attachment={{
            id: "history-0-attachment-0",
            kind: "image",
            name: "shot.png",
            previewUrl: "/api/fs/read-data-url?path=%2Ftmp%2Fshot.png",
            sizeBytes: 12,
          }}
          variant="bubble"
        />,
      );
    });
    await act(async () => {
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/fs/read-data-url?path=%2Ftmp%2Fshot.png",
      expect.objectContaining({ credentials: "include" }),
    );
    expect(container.querySelector("img")?.getAttribute("src")).toBe(dataUrl);

    await act(async () => root.unmount());
  });

  it("keeps object URLs direct for newly queued image attachments", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <MessageAttachmentCard
          attachment={{
            id: "queued-image",
            kind: "image",
            name: "shot.png",
            previewUrl: "blob:https://example.test/image",
            sizeBytes: 12,
          }}
          variant="bubble"
        />,
      );
    });

    expect(fetchMock).not.toHaveBeenCalled();
    expect(container.querySelector("img")?.getAttribute("src")).toBe(
      "blob:https://example.test/image",
    );

    await act(async () => root.unmount());
  });
});
