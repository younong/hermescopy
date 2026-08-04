// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { SessionInfo } from "@/lib/api";
import { ChatSessionList } from "./ChatSessionList";

const mocks = vi.hoisted(() => ({
  getSessions: vi.fn(),
  renameSession: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getSessions: mocks.getSessions,
      renameSession: mocks.renameSession,
    },
  };
});

vi.mock("@/i18n", () => ({
  useI18n: () => ({
    t: {
      common: {
        cancel: "Cancel",
        loading: "Loading",
        refresh: "Refresh",
        retry: "Retry",
        save: "Save",
        saving: "Saving",
      },
      sessions: {
        failedToRename: "Failed to rename session",
        newChat: "New chat",
        noMatch: "No match",
        noSessions: "No sessions",
        renameSession: "Rename session",
        sessionTitlePlaceholder: "Session title",
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
  mocks.renameSession.mockReset();
  document.body.innerHTML = "";
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  document.body.innerHTML = "";
});

describe("ChatSessionList", () => {
  it("filters compact rows and reports the active session title", async () => {
    mocks.getSessions.mockResolvedValue(sessionList([
      session("alpha", "Release notes", "Published"),
      session("beta", null, "UI exploration"),
    ]));
    const onActiveSessionChange = vi.fn();
    const container = mount();

    await render(
      <MemoryRouter>
        <ChatSessionList
          activeSessionId="beta"
          onActiveSessionChange={onActiveSessionChange}
          query="exploration"
          variant="compact"
        />
      </MemoryRouter>,
    );

    expect(container.textContent).not.toContain("Release notes");
    expect(container.textContent).toContain("UI exploration");
    expect(container.textContent).not.toContain("New chat");
    expect(container.querySelector('[aria-current="true"]')?.textContent).toContain("UI exploration");
    expect(container.querySelector('[aria-current="true"]')?.className).toContain("bg-white");
    expect(onActiveSessionChange).toHaveBeenLastCalledWith({ id: "beta", label: "UI exploration" });
    expect(mocks.getSessions).toHaveBeenCalledWith(30, 0, "recent", true);
  });

  it("opens selected sessions on an optional destination route", async () => {
    mocks.getSessions.mockResolvedValue(sessionList([
      session("alpha", "Release notes", "Published"),
    ]));
    const container = mount();

    await render(
      <MemoryRouter initialEntries={["/chat/files"]}>
        <ChatSessionList activeSessionId={null} sessionPath="/chat" variant="compact" />
        <LocationProbe />
      </MemoryRouter>,
    );
    await click(buttonWithText(container, "Release notes"));

    expect(container.querySelector("[data-location]")?.getAttribute("data-location"))
      .toBe("/chat?resume=alpha");
  });

  it("keeps the default panel chrome and exposes rename in both variants", async () => {
    mocks.getSessions.mockResolvedValue(sessionList([
      session("alpha", "Release notes", "Published"),
    ]));
    const container = mount();

    await render(
      <MemoryRouter>
        <ChatSessionList activeSessionId={null} />
      </MemoryRouter>,
    );

    expect(container.textContent).toContain("Sessions");
    expect(container.textContent).toContain("New chat");
    expect(container.textContent).toContain("3 msgs");
    expect(renameButton(container, "Release notes")).toBeTruthy();
    expect(mocks.getSessions).toHaveBeenCalledWith(30, 0, "recent", false);
  });

  it("renames within the current owner without navigating and uses the server title", async () => {
    const refresh = deferred<ReturnType<typeof sessionList>>();
    mocks.getSessions
      .mockResolvedValueOnce(sessionList([
        session("alpha", "Release notes", "Published"),
      ]))
      .mockReturnValueOnce(refresh.promise);
    mocks.renameSession.mockResolvedValue({ ok: true, title: "Server title" });
    const onActiveSessionChange = vi.fn();
    const onPicked = vi.fn();
    const onSessionPick = vi.fn();
    const container = mount();

    await render(
      <MemoryRouter initialEntries={["/chat?resume=alpha"]}>
        <ChatSessionList
          activeSessionId="alpha"
          onActiveSessionChange={onActiveSessionChange}
          onPicked={onPicked}
          onSessionPick={onSessionPick}
          sessionPath="/chat"
          variant="compact"
        />
        <LocationProbe />
      </MemoryRouter>,
    );
    await click(renameButton(container, "Release notes"));
    const input = titleInput(container);
    await changeInput(input, "  Client title  ");
    await click(buttonByLabel(container, "Save"));

    expect(mocks.renameSession).toHaveBeenCalledWith("alpha", "Client title");
    expect(container.textContent).toContain("Server title");
    expect(container.textContent).not.toContain("Client title");
    expect(container.querySelector('[aria-current="true"]')?.textContent).toContain("Server title");
    expect(onActiveSessionChange).toHaveBeenLastCalledWith({ id: "alpha", label: "Server title" });
    expect(onPicked).not.toHaveBeenCalled();
    expect(onSessionPick).not.toHaveBeenCalled();
    expect(container.querySelector("[data-location]")?.getAttribute("data-location"))
      .toBe("/chat?resume=alpha");

    await act(async () => {
      refresh.resolve(sessionList([
        session("alpha", "Stale list title", "Published"),
      ]));
      await refresh.promise;
    });
    expect(container.textContent).toContain("Server title");
    expect(container.textContent).not.toContain("Stale list title");
  });

  it("supports keyboard save and cancel without submitting empty, unchanged, or composing input", async () => {
    mocks.getSessions.mockResolvedValue(sessionList([
      session("alpha", "Release notes", "Published"),
      session("beta", null, "Preview only"),
    ]));
    mocks.renameSession.mockResolvedValue({ ok: true, title: "Keyboard title" });
    const container = mount();

    await render(
      <MemoryRouter>
        <ChatSessionList activeSessionId={null} variant="compact" />
      </MemoryRouter>,
    );

    await click(renameButton(container, "Release notes"));
    expect(titleInput(container).value).toBe("Release notes");
    await keyDown(titleInput(container), "Enter");
    expect(mocks.renameSession).not.toHaveBeenCalled();

    await click(renameButton(container, "Preview only"));
    expect(titleInput(container).value).toBe("");
    await changeInput(titleInput(container), "Keyboard title");
    await keyDown(titleInput(container), "Enter", true);
    expect(mocks.renameSession).not.toHaveBeenCalled();
    await keyDown(titleInput(container), "Escape");
    expect(mocks.renameSession).not.toHaveBeenCalled();

    await click(renameButton(container, "Preview only"));
    await changeInput(titleInput(container), "Keyboard title");
    await keyDown(titleInput(container), "Enter");
    expect(mocks.renameSession).toHaveBeenCalledWith("beta", "Keyboard title");
    expect(container.textContent).toContain("Keyboard title");
  });

  it("keeps the editor busy on save, preserves failures, and retries", async () => {
    mocks.getSessions.mockResolvedValue(sessionList([
      session("alpha", "Release notes", "Published"),
    ]));
    const pending = deferred<{ ok: boolean; title: string }>();
    mocks.renameSession
      .mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce({ ok: true, title: "Retried title" });
    const container = mount();

    await render(
      <MemoryRouter>
        <ChatSessionList activeSessionId="alpha" variant="compact" />
      </MemoryRouter>,
    );
    await click(renameButton(container, "Release notes"));
    await changeInput(titleInput(container), "Retried title");
    await click(buttonByLabel(container, "Save"));

    expect(container.querySelector('[aria-busy="true"]')).toBeTruthy();
    expect(titleInput(container).disabled).toBe(true);
    expect(buttonByLabel(container, "Save").disabled).toBe(true);
    expect(buttonByLabel(container, "Cancel").disabled).toBe(true);

    await act(async () => {
      pending.reject(new Error("rename unavailable"));
      await pending.promise.catch(() => undefined);
    });

    expect(container.querySelector('[role="alert"]')?.textContent).toContain("rename unavailable");
    expect(titleInput(container).value).toBe("Retried title");
    expect(container.textContent).toContain("rename unavailable");
    await click(buttonByLabel(container, "Save"));
    expect(container.textContent).toContain("Retried title");
    expect(container.querySelector('[role="alert"]')).toBeNull();
    expect(mocks.renameSession).toHaveBeenCalledTimes(2);
  });

});

function mount(): HTMLDivElement {
  const container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  return container;
}

async function render(node: React.ReactNode): Promise<void> {
  await act(async () => {
    root?.render(node);
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function click(button: HTMLButtonElement): Promise<void> {
  await act(async () => {
    button.click();
    await Promise.resolve();
  });
}

async function changeInput(input: HTMLInputElement, value: string): Promise<void> {
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )?.set;
    setter?.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await Promise.resolve();
  });
}

async function keyDown(
  input: HTMLInputElement,
  key: string,
  isComposing = false,
): Promise<void> {
  await act(async () => {
    input.dispatchEvent(
      new KeyboardEvent("keydown", { bubbles: true, isComposing, key }),
    );
    await Promise.resolve();
  });
}

function buttonByLabel(container: HTMLElement, label: string): HTMLButtonElement {
  const button = container.querySelector<HTMLButtonElement>(
    `button[aria-label="${label}"]`,
  );
  if (!button) throw new Error(`Missing button: ${label}`);
  return button;
}

function buttonWithText(container: HTMLElement, text: string): HTMLButtonElement {
  const button = Array.from(container.querySelectorAll<HTMLButtonElement>("button"))
    .find((candidate) => candidate.textContent?.includes(text));
  if (!button) throw new Error(`Missing button containing: ${text}`);
  return button;
}

function renameButton(container: HTMLElement, title: string): HTMLButtonElement {
  return buttonByLabel(container, `Rename session: ${title}`);
}

function titleInput(container: HTMLElement): HTMLInputElement {
  const input = container.querySelector<HTMLInputElement>(
    'input[aria-label="Session title"]',
  );
  if (!input) throw new Error("Missing title input");
  return input;
}

function LocationProbe() {
  const location = useLocation();
  return <span data-location={`${location.pathname}${location.search}`} />;
}

function sessionList(sessions: SessionInfo[]) {
  return { limit: 30, offset: 0, sessions, total: sessions.length };
}

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

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, reject, resolve };
}
