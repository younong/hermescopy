// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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


  it("automatically loads near the top and keeps manual loading for errors only", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const onLoadEarlier = vi.fn();
    const baseState = {
      ...initialGuiChatState,
      historyCursor: "cursor-1",
      historyHasMore: true,
      messages: [{ artifactIds: [], id: "message-1", role: "user" as const, text: "Hello" }],
      sessionId: "session-1",
    };

    await act(async () => root.render(
      <MessageList
        onApprovalRespond={() => undefined}
        onLoadEarlier={onLoadEarlier}
        state={baseState}
      />,
    ));

    expect(container.textContent).toContain("Scroll up for earlier messages");
    expect(container.textContent).not.toContain("Load earlier messages");
    const scroller = container.querySelector<HTMLElement>("[aria-busy=false]")!;
    await act(async () => {
      scroller.scrollTop = 300;
      scroller.dispatchEvent(new Event("scroll", { bubbles: true }));
      scroller.scrollTop = 100;
      scroller.dispatchEvent(new Event("scroll", { bubbles: true }));
    });
    expect(onLoadEarlier).toHaveBeenCalledTimes(1);

    await act(async () => root.render(
      <MessageList
        onApprovalRespond={() => undefined}
        onLoadEarlier={onLoadEarlier}
        state={{ ...baseState, historyLoading: true }}
      />,
    ));
    await act(async () => root.render(
      <MessageList
        onApprovalRespond={() => undefined}
        onLoadEarlier={onLoadEarlier}
        state={{ ...baseState, historyError: "Network unavailable" }}
      />,
    ));
    const retry = Array.from(container.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("Retry loading earlier messages"),
    );
    expect(container.querySelector('[role="alert"]')?.textContent).toBe("Network unavailable");
    expect(retry).toBeDefined();

    await act(async () => retry?.click());
    expect(onLoadEarlier).toHaveBeenCalledTimes(2);
    await act(async () => root.unmount());
  });

  it("announces earlier-history loading without hiding messages", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => root.render(
      <MessageList
        onApprovalRespond={() => undefined}
        state={{
          ...initialGuiChatState,
          historyCursor: "cursor-1",
          historyHasMore: true,
          historyLoading: true,
          messages: [{ artifactIds: [], id: "message-1", role: "user", text: "Still visible" }],
        }}
      />,
    ));

    expect(container.querySelector('[role="status"]')?.textContent).toContain("Loading earlier messages");
    expect(container.textContent).toContain("Still visible");
    expect(container.querySelector("[aria-busy=true]")).not.toBeNull();
    await act(async () => root.unmount());
  });

});
