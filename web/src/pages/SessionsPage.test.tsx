// @vitest-environment jsdom

import { act } from "react";
import { createRoot } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import SessionsPage, {
  fetchSessionsOverview,
  scheduleSessionsOverviewPoll,
  SessionMessageList,
} from "./SessionsPage";

vi.mock("@/contexts/usePageHeader", () => ({
  usePageHeader: () => ({ setAfterTitle: vi.fn(), setEnd: vi.fn(), setTitle: vi.fn() }),
}));
vi.mock("@/contexts/useSystemActions", () => ({
  useSystemActions: () => ({ activeAction: null, actionStatus: null, dismissLog: vi.fn() }),
}));

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

    expect(getSessions).toHaveBeenCalledWith(30, 0, "recent", true);
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

function session(id: string) {
  return {
    id,
    source: "cli",
    model: "test/model",
    title: id,
    started_at: 1,
    ended_at: 2,
    last_active: 2,
    is_active: false,
    message_count: 1,
    tool_call_count: 0,
    input_tokens: 0,
    output_tokens: 0,
    preview: "preview",
  };
}

function mockSessionsPageApi() {
  vi.spyOn(api, "getSessions").mockResolvedValue({
    limit: 20,
    offset: 0,
    sessions: [session("session-a")],
    total: 1,
  });
  vi.spyOn(api, "getSessionComposition").mockResolvedValue({
    scope: {
      requested_ids: ["session-a"],
      canonical_session_count: 1,
      canonical_root_ids: ["session-a"],
      canonical_tip_ids: ["session-a"],
      aggregation: "full_canonical_lineages",
      date_truncation: false,
    },
    charts: [],
    coverage: {},
    limitations: [],
  });
  vi.spyOn(api, "getStatus").mockResolvedValue({
    active_sessions: 0,
    config_path: "",
    config_version: 1,
    env_path: "",
    gateway_exit_reason: null,
    gateway_health_url: null,
    gateway_pid: null,
    gateway_platforms: {},
    gateway_running: false,
    gateway_state: null,
    gateway_updated_at: null,
    hermes_home: "",
    latest_config_version: 1,
    release_date: "",
    version: "",
  });
  vi.spyOn(api, "getSessionStats").mockResolvedValue({
    total: 1,
    active_store: 0,
    archived: 0,
    messages: 1,
    by_source: {},
  });
  vi.spyOn(api, "getEmptySessionsCount").mockResolvedValue({ count: 0 });
  vi.spyOn(api, "getSessionMessages").mockResolvedValue({
    session_id: "session-a",
    messages: [],
  });
}

async function renderSessionsPage() {
  const container = document.createElement("div");
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(<MemoryRouter><SessionsPage /></MemoryRouter>);
    await Promise.resolve();
    await Promise.resolve();
  });
  return { container, root };
}

function click(element: Element) {
  element.dispatchEvent(new MouseEvent("click", { bubbles: true }));
}

describe("SessionsPage composition flow", () => {
  it("resets selection and expansion on date change and requests aggregate/detail composition without dates", async () => {
    mockSessionsPageApi();
    const { container, root } = await renderSessionsPage();
    const history = Array.from(container.querySelectorAll('[role="radio"]')).find((button) => button.textContent === "History")!;
    await act(async () => click(history));

    await vi.waitFor(() => {
      expect(container.querySelector('[role="checkbox"]'), container.innerHTML).not.toBeNull();
    });
    const checkbox = container.querySelector('[role="checkbox"]')!;
    await act(async () => click(checkbox));
    expect(api.getSessionComposition).toHaveBeenCalledWith(["session-a"], expect.objectContaining({ signal: expect.any(AbortSignal) }));

    const row = Array.from(container.querySelectorAll("div")).find((node) =>
      node.textContent?.includes("session-a") && node.className.includes("cursor-pointer"),
    )!;
    await act(async () => click(row));
    expect(api.getSessionComposition).toHaveBeenCalledTimes(2);

    const allTime = Array.from(container.querySelectorAll("button")).find((button) => button.textContent === "All time")!;
    await act(async () => click(allTime));
    await vi.waitFor(() => expect(container.textContent).not.toContain("1 selected"));
    expect(api.getSessions).toHaveBeenCalledWith(
      20,
      0,
      "created",
      false,
      expect.objectContaining({ signal: expect.any(AbortSignal) }),
    );
    const filteredCalls = vi.mocked(api.getSessions).mock.calls.filter(([limit]) => limit === 20);
    expect(filteredCalls.at(-1)?.[4]).not.toHaveProperty("active_from");
    expect(filteredCalls.at(-1)?.[4]).not.toHaveProperty("active_before");
    for (const [ids, options] of vi.mocked(api.getSessionComposition).mock.calls) {
      expect(ids).toEqual(["session-a"]);
      expect(options).not.toHaveProperty("active_from");
      expect(options).not.toHaveProperty("active_before");
    }
    await act(async () => root.unmount());
  });
});

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
