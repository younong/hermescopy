// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { ChatMessage, FileArtifactState } from "../../types";
import { MessageBubble } from "../MessageBubble";

afterEach(() => {
  document.body.innerHTML = "";
  vi.restoreAllMocks();
});

describe("MessageBubble", () => {
  it("routes collaboration cards within the configured dashboard base path", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <MemoryRouter basename="/hermes" initialEntries={["/hermes/chat?resume=session-a"]}>
          <MessageBubble
            artifacts={[]}
            message={{
              artifactIds: [],
              collaborationCard: {
                groupId: "group-a",
                status: "created",
                taskId: "task-a",
                title: "Review",
                text: "Started",
              },
              id: "card-a",
              role: "system",
              text: "",
            }}
          />
        </MemoryRouter>,
      );
    });

    expect(container.querySelector("a")?.getAttribute("href")).toBe(
      "/hermes/chat?group=group-a",
    );
    await act(async () => root.unmount());
  });

  it("renders user messages as the quiet right-aligned bubble", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <MessageBubble
          artifacts={[]}
          message={{ artifactIds: [], id: "user-1", role: "user", text: "Hello" }}
        />,
      );
    });

    expect(container.querySelector('[data-message-variant="user"]')?.textContent).toBe("Hello");
    expect(container.querySelector("article")?.className).toContain("justify-end");
    await act(async () => root.unmount());
  });

  it("renders assistant Markdown while streaming and catches up on completion", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const message: ChatMessage = {
      artifactIds: [],
      id: "assistant-1",
      role: "assistant",
      streaming: true,
      text:
        "## Streaming reply\n\n" +
        "**Bold text** with `inline code`.\n\n" +
        "- First item\n\n" +
        "| Platform | Direction |\n|---|---|\n| Bilibili | Technology |",
    };

    await act(async () => {
      root.render(<MessageBubble artifacts={[]} message={message} />);
    });

    expect(container.querySelector("h2")?.textContent).toContain("Streaming reply");
    expect(container.querySelector("strong")?.textContent).toBe("Bold text");
    expect(container.querySelector("p code")?.textContent).toBe("inline code");
    expect(container.querySelector("li")?.textContent).toContain("First item");
    expect(container.querySelector("table")?.textContent).toContain("Technology");
    expect(container.querySelector("[data-markdown-streaming=\"true\"]")).not.toBeNull();
    expect(container.textContent).not.toContain("## Streaming reply");
    expect(container.textContent).not.toContain("**Bold text**");

    await act(async () => {
      root.render(
        <MessageBubble
          artifacts={[]}
          message={{
            ...message,
            status: "complete",
            streaming: false,
            text: `${message.text}\n\n\`\`\`ts\nconst complete = true;\n\`\`\``,
          }}
        />,
      );
    });

    expect(container.querySelector("pre code")?.textContent).toBe("const complete = true;\n");
    expect(container.querySelector("[data-markdown-streaming]")).toBeNull();
    expect(container.querySelector("table")?.textContent).toContain("Technology");
    expect(container.textContent).not.toContain("```ts");

    await act(async () => root.unmount());
  });

  it("downloads a recognized historical ZIP from its inline Markdown link", async () => {
    const downloadUrl =
      "/api/files/download?path=%E5%85%AC%E4%BC%97%E5%8F%B7%E6%95%B0%E6%8D%AE%E8%87%AA%E5%8A%A8%E6%B8%85%E6%B4%97_%E5%8F%AF%E7%9B%B4%E6%8E%A5%E4%BD%BF%E7%94%A8.zip&cwd=%2Fworkspace&filename=%E5%85%AC%E4%BC%97%E5%8F%B7%E6%95%B0%E6%8D%AE%E8%87%AA%E5%8A%A8%E6%B8%85%E6%B4%97_%E5%8F%AF%E7%9B%B4%E6%8E%A5%E4%BD%BF%E7%94%A8.zip";
    const artifact: FileArtifactState = {
      downloadUrl,
      id: "assistant-zip-file-0",
      kind: "file",
      mimeType: "application/zip",
      name: "公众号数据自动清洗_可直接使用.zip",
      sourcePath: "公众号数据自动清洗_可直接使用.zip",
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("zip-bytes", { status: 200 }),
    );
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:generated-zip");
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);
    const downloadClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <MessageBubble
          artifacts={[artifact]}
          message={{
            artifactIds: [artifact.id],
            id: "assistant-zip",
            role: "assistant",
            text: "[下载：公众号数据自动清洗_可直接使用.zip](公众号数据自动清洗_可直接使用.zip)",
          }}
        />,
      );
    });

    const inlineLink = Array.from(container.querySelectorAll<HTMLAnchorElement>("a")).find(
      (link) => link.textContent?.includes("下载：公众号数据自动清洗_可直接使用.zip"),
    );
    expect(inlineLink).not.toBeUndefined();
    expect(inlineLink?.getAttribute("href")).toBe(downloadUrl);
    expect(inlineLink?.querySelector(".lucide-file-archive")).not.toBeNull();

    await act(async () => {
      inlineLink?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(fetchMock).toHaveBeenCalledWith(
      downloadUrl,
      expect.objectContaining({ credentials: "include" }),
    );
    expect(downloadClick).toHaveBeenCalledOnce();
    await act(async () => root.unmount());
  });

  it("keeps unrecognized relative links inert and remote links external", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <MessageBubble
          artifacts={[]}
          message={{
            artifactIds: [],
            id: "assistant-links",
            role: "assistant",
            text: "[本地管理页](/admin) [文档](https://example.com/docs)",
          }}
        />,
      );
    });

    const links = Array.from(container.querySelectorAll("a"));
    expect(links).toHaveLength(1);
    expect(links[0]?.getAttribute("href")).toBe("https://example.com/docs");
    expect(links[0]?.target).toBe("_blank");
    expect(container.textContent).toContain("本地管理页");
    await act(async () => root.unmount());
  });
});
