// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FetchJSONError, api, fetchSessionReaderJSON } from "../api";

function response(status: number, body: unknown, retryAfter?: string): Response {
  const headers = new Headers({ "Content-Type": "application/json" });
  if (retryAfter !== undefined) headers.set("Retry-After", retryAfter);
  return new Response(JSON.stringify(body), { status, headers });
}

beforeEach(() => {
  vi.useFakeTimers();
  window.__HERMES_AUTH_REQUIRED__ = true;
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  delete window.__HERMES_AUTH_REQUIRED__;
});

describe("fetchSessionReaderJSON", () => {
  it("retries one explicit Reader-unavailable response and preserves success payload", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        response(503, { error: "session_reader_unavailable" }, "0.2"),
      )
      .mockResolvedValueOnce(response(200, { sessions: ["ready"] }));

    const pending = fetchSessionReaderJSON<{ sessions: string[] }>("/api/sessions");
    await vi.advanceTimersByTimeAsync(199);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);

    await expect(pending).resolves.toEqual({ sessions: ["ready"] });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("stops after one retry and clamps Retry-After to one second", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        response(503, { error: "session_reader_unavailable" }, "30"),
      );

    const pending = fetchSessionReaderJSON("/api/sessions");
    const rejection = expect(pending).rejects.toMatchObject({
      status: 503,
    });
    await vi.advanceTimersByTimeAsync(999);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);

    await rejection;
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not retry other failures", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(response(503, { error: "maintenance" }, "0.1"));

    await expect(fetchSessionReaderJSON("/api/sessions")).rejects.toBeInstanceOf(
      FetchJSONError,
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("cancels a pending retry through AbortSignal", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      response(503, { error: "session_reader_unavailable" }, "1"),
    );
    const controller = new AbortController();
    const pending = fetchSessionReaderJSON("/api/sessions", {
      signal: controller.signal,
    });
    const rejection = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    await vi.advanceTimersByTimeAsync(1);
    controller.abort();

    await rejection;
  });

  it("uses the shared ten-message default for session history", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      response(200, { messages: [], session_id: "session-1" }),
    );

    await api.getSessionMessages("session-1");

    expect(fetchMock.mock.calls[0]?.[0]).toContain(
      "/api/sessions/session-1/messages?limit=10",
    );
  });

  it.each([200, 201, 10_000])("caps session history limit %i at 200", async (limit) => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      response(200, { messages: [], session_id: "session-1" }),
    );

    await api.getSessionMessages("session-1", { limit });

    expect(fetchMock.mock.calls[0]?.[0]).toContain(
      "/api/sessions/session-1/messages?limit=200",
    );
  });

  it("forwards getSessionMessages cancellation through a pending Reader retry", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      response(503, { error: "session_reader_unavailable" }, "1"),
    );
    const controller = new AbortController();
    const pending = api.getSessionMessages(
      "session-1",
      { limit: 42, signal: controller.signal },
    );
    const rejection = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    await vi.advanceTimersByTimeAsync(1);
    controller.abort();

    await rejection;
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0]?.[0]).toContain(
      "/api/sessions/session-1/messages?limit=42",
    );
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ signal: controller.signal });
  });

  it("encodes repeated composition IDs and forwards abort through retry", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      response(503, { error: "session_reader_unavailable" }, "1"),
    );
    const controller = new AbortController();
    const pending = api.getSessionComposition(
      ["session a", "session/b"],
      { signal: controller.signal },
    );
    const rejection = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    await vi.advanceTimersByTimeAsync(1);
    controller.abort();

    await rejection;
    const url = new URL(String(fetchMock.mock.calls[0]?.[0]), window.location.origin);
    expect(url.pathname).toContain("/api/sessions/composition");
    expect(url.searchParams.getAll("ids")).toEqual(["session a", "session/b"]);
    expect(url.searchParams.has("profile")).toBe(false);
    expect(fetchMock.mock.calls[0]?.[1]).toMatchObject({ signal: controller.signal });
  });

  it("encodes inclusive/exclusive dates for list and search without changing defaults", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(async () =>
      response(200, { sessions: [], results: [], total: 0, limit: 20, offset: 0 }),
    );

    await api.getSessions(20, 0, "created", false, {
      active_from: 100,
      active_before: 200,
    });
    await api.searchSessions("hello world", undefined, {
      active_from: 100,
      active_before: 200,
    });

    const listUrl = new URL(String(fetchMock.mock.calls[0]?.[0]), window.location.origin);
    const searchUrl = new URL(String(fetchMock.mock.calls[1]?.[0]), window.location.origin);
    expect(listUrl.searchParams.get("active_from")).toBe("100");
    expect(listUrl.searchParams.get("active_before")).toBe("200");
    expect(searchUrl.searchParams.get("q")).toBe("hello world");
    expect(searchUrl.searchParams.get("active_from")).toBe("100");
    expect(searchUrl.searchParams.get("active_before")).toBe("200");
  });

  it("soft archives session-list removals on the non-retrying JSON path", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(response(200, { archived: true, ok: true, title: "" }))
      .mockResolvedValueOnce(
        response(503, { error: "session_reader_unavailable" }, "0.1"),
      );

    await api.archiveSession("session/1");
    const [url, options] = fetchMock.mock.calls[0] ?? [];
    expect(new URL(String(url), window.location.origin).pathname).toContain(
      "/api/sessions/session%2F1",
    );
    expect(options).toMatchObject({
      body: JSON.stringify({ archived: true }),
      method: "PATCH",
    });

    await expect(api.deleteSession("session-1")).rejects.toMatchObject({
      status: 503,
    });
    await vi.runAllTimersAsync();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
