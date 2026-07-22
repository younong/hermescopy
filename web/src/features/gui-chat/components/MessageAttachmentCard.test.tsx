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
  it("keeps uploaded image geometry while its preview resolves", async () => {
    let resolveFetch!: (response: Response) => void;
    vi.spyOn(globalThis, "fetch").mockReturnValue(
      new Promise<Response>((resolve) => {
        resolveFetch = resolve;
      }),
    );
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <MessageAttachmentCard
          attachment={{
            height: 300,
            id: "history-image",
            kind: "image",
            name: "shot.png",
            previewUrl: "/api/fs/read-data-url?path=shot.png",
            sizeBytes: 12,
            width: 500,
          }}
          variant="bubble"
        />,
      );
    });

    const geometry = container.querySelector<HTMLElement>('[data-image-geometry="500x300"]');
    expect(geometry?.style.aspectRatio).toBe("500 / 300");
    expect(container.querySelector("img")).toBeNull();

    await act(async () => {
      resolveFetch(new Response(JSON.stringify({ dataUrl: "data:image/png;base64,AAAA" }), {
        headers: { "Content-Type": "application/json" },
      }));
      await Promise.resolve();
    });

    expect(container.querySelector('[data-image-geometry="500x300"]')).toBe(geometry);
    const image = container.querySelector("img");
    expect(image?.getAttribute("width")).toBe("500");
    expect(image?.getAttribute("height")).toBe("300");

    await act(async () => root.unmount());
  });

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

  it("shows and executes an explicit download action for bubble images", async () => {
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
        <MessageAttachmentCard
          attachment={{
            downloadUrl: "/api/files/download?path=%2Ftmp%2Fshot.png",
            id: "history-0-attachment-0",
            kind: "image",
            name: "shot.png",
            previewUrl: "data:image/png;base64,iVBORw0KGgo=",
            sizeBytes: 12,
          }}
          variant="bubble"
        />,
      );
    });

    const download = container.querySelector('a[aria-label="Download shot.png"]');
    expect(download?.textContent).toContain("Download");

    await act(async () => {
      download?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/files/download?path=%2Ftmp%2Fshot.png",
      expect.objectContaining({ credentials: "include" }),
    );
    expect(click).toHaveBeenCalledOnce();

    await act(async () => root.unmount());
  });

  it("renders type-specific icons and a download action", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <MessageAttachmentCard
          attachment={{
            downloadUrl: "/api/files/download?path=report.html",
            id: "html-file",
            kind: "file",
            mimeType: "text/html",
            name: "report.html",
            sizeBytes: 42,
          }}
        />,
      );
    });

    expect(container.querySelector('[data-file-type="html"]')).not.toBeNull();
    expect(container.querySelector("a")?.getAttribute("aria-label")).toBe("Download report.html");

    await act(async () => root.unmount());
  });

  it("shows a failed download instead of swallowing it", async () => {
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
        <MessageAttachmentCard
          attachment={{
            downloadUrl: "/api/files/download?path=missing.txt",
            id: "missing-file",
            kind: "file",
            name: "missing.txt",
            sizeBytes: 42,
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

    expect(container.querySelector('[role="alert"]')?.textContent).toContain(
      "Download failed (404): Path not found",
    );
    expect(container.querySelector("a")?.getAttribute("aria-busy")).toBe("false");

    await act(async () => root.unmount());
  });

  it("shows legacy PDFs without an original path as unavailable", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <MessageAttachmentCard
          attachment={{ id: "legacy-pdf", kind: "pdf", name: "old.pdf", sizeBytes: 42 }}
        />,
      );
    });

    expect(container.querySelector('[data-file-type="pdf"]')).not.toBeNull();
    expect(container.querySelector("a")).toBeNull();
    expect(container.querySelector('[aria-disabled="true"]')).not.toBeNull();

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
