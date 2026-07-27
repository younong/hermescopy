// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import {
  fetchSessionsOverview,
  scheduleSessionsOverviewPoll,
  SessionMessageList,
} from "./SessionsPage";

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  document.body.innerHTML = "";
});

afterEach(() => {
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("fetchSessionsOverview", () => {
  it("requests compact recent session metadata", async () => {
    const getSessions = vi.spyOn(api, "getSessions").mockResolvedValue({
      limit: 30,
      offset: 0,
      sessions: [],
      total: 0,
    });

    await fetchSessionsOverview();

    expect(getSessions).toHaveBeenCalledWith(30, 0, undefined, "recent", true);
  });
});

describe("scheduleSessionsOverviewPoll", () => {
  it("does not overlap polls when one request is still pending", async () => {
    vi.useFakeTimers();
    let resolveFirst: (() => void) | undefined;
    const poll = vi
      .fn<() => Promise<void>>()
      .mockImplementationOnce(
        () => new Promise<void>((resolve) => {
          resolveFirst = resolve;
        }),
      )
      .mockResolvedValue(undefined);

    const stop = scheduleSessionsOverviewPoll(poll);
    await vi.advanceTimersByTimeAsync(15_000);
    expect(poll).toHaveBeenCalledTimes(1);

    resolveFirst?.();
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(4_999);
    expect(poll).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(poll).toHaveBeenCalledTimes(2);

    stop();
    vi.useRealTimers();
  });
});

function scroll(element: HTMLElement, scrollTop: number) {
  element.scrollTop = scrollTop;
  element.dispatchEvent(new Event("scroll", { bubbles: true }));
}

describe("SessionMessageList", () => {
  it("loads earlier messages on upward scrolling but not at initial top", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const onLoadEarlier = vi.fn();

    await act(async () => root.render(
      <SessionMessageList
        canLoadEarlier
        historyLoading={false}
        messages={[{ text: "Current message", role: "user" }]}
        onLoadEarlier={onLoadEarlier}
        sessionId="session-1"
      />,
    ));
    const scroller = container.querySelector<HTMLElement>("[aria-busy=false]")!;
    expect(container.textContent).toContain("Scroll up for earlier messages");
    expect(container.textContent).not.toContain("Load earlier messages");

    await act(async () => scroll(scroller, 0));
    await act(async () => scroll(scroller, 300));
    await act(async () => scroll(scroller, 100));
    await act(async () => scroll(scroller, 80));

    expect(onLoadEarlier).toHaveBeenCalledTimes(1);
    await act(async () => root.unmount());
  });

  it("keeps messages visible while loading and offers retry only after an error", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const onLoadEarlier = vi.fn();
    const messages = [{ text: "Current message", role: "assistant" as const }];

    await act(async () => root.render(
      <SessionMessageList
        canLoadEarlier
        historyLoading
        messages={messages}
        onLoadEarlier={onLoadEarlier}
        sessionId="session-1"
      />,
    ));
    expect(container.querySelector('[role="status"]')?.textContent).toContain("Loading earlier messages");
    expect(container.textContent).toContain("Current message");

    await act(async () => root.render(
      <SessionMessageList
        canLoadEarlier
        historyError="Network unavailable"
        historyLoading={false}
        messages={messages}
        onLoadEarlier={onLoadEarlier}
        sessionId="session-1"
      />,
    ));
    expect(container.querySelector('[role="alert"]')?.textContent).toBe("Network unavailable");
    const retry = Array.from(container.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("Retry loading earlier messages"),
    );
    expect(retry).toBeDefined();
    await act(async () => retry?.click());
    expect(onLoadEarlier).toHaveBeenCalledTimes(1);
    await act(async () => root.unmount());
  });

  it("preserves the reading position after prepending messages", async () => {
    const container = document.createElement("div");
    document.body.appendChild(container);
    const root = createRoot(container);
    const onLoadEarlier = vi.fn();
    let scrollHeight = 900;

    await act(async () => root.render(
      <SessionMessageList
        canLoadEarlier
        historyLoading={false}
        messages={[{ text: "Current message", role: "user" }]}
        onLoadEarlier={onLoadEarlier}
        sessionId="session-1"
      />,
    ));
    const scroller = container.querySelector<HTMLElement>("[aria-busy=false]")!;
    Object.defineProperty(scroller, "scrollHeight", {
      configurable: true,
      get: () => scrollHeight,
    });
    scroller.scrollTop = 300;
    await act(async () => scroll(scroller, 300));
    await act(async () => scroll(scroller, 100));
    expect(onLoadEarlier).toHaveBeenCalledTimes(1);

    await act(async () => root.render(
      <SessionMessageList
        canLoadEarlier
        historyLoading
        messages={[{ text: "Current message", role: "user" }]}
        onLoadEarlier={onLoadEarlier}
        sessionId="session-1"
      />,
    ));
    scrollHeight = 1200;
    await act(async () => root.render(
      <SessionMessageList
        canLoadEarlier
        historyLoading={false}
        messages={[
          { text: "Earlier message", role: "assistant" },
          { text: "Current message", role: "user" },
        ]}
        onLoadEarlier={onLoadEarlier}
        sessionId="session-1"
      />,
    ));

    expect(scroller.scrollTop).toBe(400);
    await act(async () => root.unmount());
  });

});
