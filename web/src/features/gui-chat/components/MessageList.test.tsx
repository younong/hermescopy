// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { initialGuiChatState } from "../types";
import { MessageList } from "./MessageList";

const resizeObservers: TestResizeObserver[] = [];
let contentHeight = 0;

class TestResizeObserver implements ResizeObserver {
  readonly callback: ResizeObserverCallback;
  readonly targets = new Set<Element>();

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
    resizeObservers.push(this);
  }

  disconnect() {
    this.targets.clear();
  }

  observe(target: Element) {
    this.targets.add(target);
  }

  unobserve(target: Element) {
    this.targets.delete(target);
  }
}

function resize(element: Element, blockSize: number) {
  resizeObservers.find((observer) => observer.targets.has(element))?.callback(
    [{
      borderBoxSize: [{ blockSize, inlineSize: 800 }],
      target: element,
    } as unknown as ResizeObserverEntry],
    {} as ResizeObserver,
  );
}

function scroll(element: HTMLElement, scrollTop: number) {
  element.scrollTop = scrollTop;
  element.dispatchEvent(new Event("scroll", { bubbles: true }));
}

function waitForFrame() {
  return new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
}

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = "";
  resizeObservers.length = 0;
  contentHeight = 0;
  vi.stubGlobal("ResizeObserver", TestResizeObserver);
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) =>
    setTimeout(() => callback(0), 0));
  vi.stubGlobal("cancelAnimationFrame", (handle: number) => clearTimeout(handle));
  Object.defineProperties(HTMLElement.prototype, {
    clientHeight: { configurable: true, get: () => 600 },
    offsetHeight: {
      configurable: true,
      get() {
        return this.hasAttribute("data-index") ? 140 : 600;
      },
    },
    offsetWidth: { configurable: true, get: () => 800 },
    scrollHeight: {
      configurable: true,
      get() {
        return contentHeight || 600;
      },
    },
    scrollTo: {
      configurable: true,
      value(options: ScrollToOptions) {
        if (typeof options.top === "number") {
          this.scrollTop = Math.min(options.top, Math.max(0, this.scrollHeight - this.offsetHeight));
        }
      },
    },
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
        onClarifyRespond={() => undefined}
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

  it("keeps image history pinned only while the user is at the bottom", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const messages = Array.from({ length: 8 }, (_, index) => ({
      artifactIds: index === 7 ? ["generated-image"] : [],
      id: `message-${index}`,
      role: "assistant" as const,
      text: `Reply ${index}`,
    }));

    await act(async () => root.render(
      <MessageList
        forceBottomKey="history-session"
        onClarifyRespond={() => undefined}
        onApprovalRespond={() => undefined}
        state={{
          ...initialGuiChatState,
          artifacts: {
            "generated-image": {
              id: "generated-image",
              messageId: "message-7",
              title: "Generated image",
              toolCallId: "tool-image",
              url: "data:image/png;base64,AAAA",
            },
          },
          messages,
          sessionId: "history-session",
        }}
      />,
    ));

    const scroller = container.querySelector<HTMLElement>("[aria-busy=false]")!;
    const imageRow = container.querySelector<HTMLElement>('[data-index="7"]')!;
    contentHeight = 1120;
    await act(async () => scroll(scroller, 520));

    await act(async () => {
      resize(imageRow, 340);
    });
    contentHeight = 1320;
    await act(async () => waitForFrame());
    expect(scroller.scrollTop).toBe(720);

    await act(async () => waitForFrame());
    await act(async () => scroll(scroller, 400));
    await act(async () => {
      resize(imageRow, 440);
      contentHeight = 1420;
    });
    expect(scroller.scrollTop).toBe(400);

    await act(async () => root.unmount());
  });

  it("renders a message-owned generated image before later conversation messages", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);

    await act(async () => {
      root.render(
        <MessageList
        onClarifyRespond={() => undefined}
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


  it("renders and answers a pending clarification", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const onClarifyRespond = vi.fn();

    await act(async () => {
      root.render(
        <MessageList
          onApprovalRespond={() => undefined}
          onClarifyRespond={onClarifyRespond}
          state={{
            ...initialGuiChatState,
            clarificationOrder: ["clarify-1"],
            clarifications: {
              "clarify-1": {
                choices: ["A", "B"],
                expiresAtMs: Date.now() + 60_000,
                id: "clarify-1",
                question: "Pick one",
                status: "pending",
              },
            },
          }}
        />,
      );
    });

    expect(container.textContent).toContain("Hermes needs your answer");
    expect(container.textContent).toContain("Pick one");
    const choice = Array.from(container.querySelectorAll("button")).find(
      (button) => button.textContent === "B",
    );
    await act(async () => choice?.click());
    expect(onClarifyRespond).toHaveBeenCalledWith("clarify-1", "B");

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
        onClarifyRespond={() => undefined}
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
        onClarifyRespond={() => undefined}
        onApprovalRespond={() => undefined}
        onLoadEarlier={onLoadEarlier}
        state={{ ...baseState, historyLoading: true }}
      />,
    ));
    await act(async () => root.render(
      <MessageList
        onClarifyRespond={() => undefined}
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
        onClarifyRespond={() => undefined}
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
