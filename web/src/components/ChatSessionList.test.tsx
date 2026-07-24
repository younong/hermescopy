// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SessionInfo } from "@/lib/api";
import { ChatSessionList } from "./ChatSessionList";

const mocks = vi.hoisted(() => ({ getSessions: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, getSessions: mocks.getSessions } };
});

vi.mock("@/i18n", () => ({
  useI18n: () => ({
    t: {
      common: { loading: "Loading", refresh: "Refresh", retry: "Retry" },
      sessions: {
        newChat: "New chat",
        noMatch: "No match",
        noSessions: "No sessions",
        title: "Sessions",
        untitledSession: "Untitled",
      },
    },
  }),
}));

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean })
    .IS_REACT_ACT_ENVIRONMENT = true;
  mocks.getSessions.mockReset();
  document.body.innerHTML = "";
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
});

describe("ChatSessionList", () => {
  it("filters compact rows and reports the active session title", async () => {
    mocks.getSessions.mockResolvedValue({
      limit: 30,
      offset: 0,
      sessions: [session("alpha", "Release notes", "Published"), session("beta", null, "UI exploration")],
      total: 2,
    });
    const onActiveSessionChange = vi.fn();
    const container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(
        <MemoryRouter>
          <ChatSessionList
            activeSessionId="beta"
            onActiveSessionChange={onActiveSessionChange}
            query="exploration"
            variant="compact"
          />
        </MemoryRouter>,
      );
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.textContent).not.toContain("Release notes");
    expect(container.textContent).toContain("UI exploration");
    expect(container.textContent).not.toContain("New chat");
    expect(container.querySelector('[aria-current="true"]')?.textContent).toContain("UI exploration");
    expect(onActiveSessionChange).toHaveBeenLastCalledWith({ id: "beta", label: "UI exploration" });
  });

  it("keeps the default panel chrome and metadata", async () => {
    mocks.getSessions.mockResolvedValue({
      limit: 30,
      offset: 0,
      sessions: [session("alpha", "Release notes", "Published")],
      total: 1,
    });
    const container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);

    await act(async () => {
      root?.render(
        <MemoryRouter>
          <ChatSessionList activeSessionId={null} />
        </MemoryRouter>,
      );
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(container.textContent).toContain("Sessions");
    expect(container.textContent).toContain("New chat");
    expect(container.textContent).toContain("3 msgs");
  });
});

function session(id: string, title: string | null, preview: string | null): SessionInfo {
  return {
    ended_at: null,
    id,
    input_tokens: 0,
    is_active: false,
    last_active: Date.now(),
    message_count: 3,
    model: "test-model",
    output_tokens: 0,
    preview,
    source: "gui",
    started_at: Date.now(),
    title,
    tool_call_count: 0,
  };
}
