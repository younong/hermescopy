// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { initialGuiChatState } from "../types";
import { MessageList } from "./MessageList";

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = "";
  Object.defineProperties(HTMLElement.prototype, {
    offsetHeight: { configurable: true, get: () => 600 },
    offsetWidth: { configurable: true, get: () => 800 },
  });
});

afterEach(() => {
  document.body.innerHTML = "";
});

describe("MessageList", () => {
  it("hides intermediate tool details while preserving generated artifacts", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <MessageList
          onApprovalRespond={() => undefined}
          state={{
            ...initialGuiChatState,
            artifacts: {
              "generated-report": {
                downloadUrl: "/api/files/download?path=%2Fworkspace%2Freport.html",
                id: "generated-report",
                kind: "file",
                name: "report.html",
                sourcePath: "/workspace/report.html",
              },
            },
            toolCalls: {
              "tool-1": {
                artifactIds: ["generated-report"],
                argsText: '{"path":"/workspace/report.html"}',
                id: "tool-1",
                name: "write_file",
                output: "Created /workspace/report.html",
                status: "succeeded",
              },
            },
            toolOrder: ["tool-1"],
          }}
        />,
      );
    });

    expect(container.textContent).not.toContain("write_file");
    expect(container.textContent).not.toContain("Created /workspace/report.html");
    expect(container.textContent).toContain("report.html");
    expect(
      container.querySelector('a[href="/api/files/download?path=%2Fworkspace%2Freport.html"]'),
    ).not.toBeNull();

    await act(async () => root.unmount());
  });

  it("renders a message-owned generated image before later conversation messages", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <MessageList
          onApprovalRespond={() => undefined}
          state={{
            ...initialGuiChatState,
            artifacts: {
              "generated-image": {
                id: "generated-image",
                messageId: "assistant-image",
                title: "Generated image",
                toolCallId: "tool-image",
                url: "data:image/png;base64,AAAA",
              },
            },
            messages: [
              {
                artifactIds: ["generated-image"],
                id: "assistant-image",
                role: "assistant",
                text: "First reply",
              },
              {
                artifactIds: [],
                id: "assistant-later",
                role: "assistant",
                text: "Later reply",
              },
            ],
            toolCalls: {
              "tool-image": {
                artifactIds: [],
                id: "tool-image",
                name: "image_generate",
                output: "",
                status: "succeeded",
              },
            },
            toolOrder: ["tool-image"],
          }}
        />,
      );
    });

    const image = container.querySelector('img[alt="Generated image"]');
    expect(image).not.toBeNull();
    expect(container.querySelectorAll('img[alt="Generated image"]')).toHaveLength(1);
    const articles = Array.from(container.querySelectorAll("article"));
    const firstReply = articles.find((article) => article.textContent?.includes("First reply"));
    const laterReply = articles.find((article) => article.textContent?.includes("Later reply"));
    expect(firstReply?.contains(image)).toBe(true);
    expect(firstReply).toBeDefined();
    expect(laterReply).toBeDefined();
    expect(
      firstReply && laterReply
        ? firstReply.compareDocumentPosition(laterReply) & Node.DOCUMENT_POSITION_FOLLOWING
        : 0,
    ).toBeTruthy();

    await act(async () => root.unmount());
  });

});
