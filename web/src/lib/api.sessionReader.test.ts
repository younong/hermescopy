// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FetchJSONError, api, fetchSessionReaderJSON } from "./api";

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

  it("keeps session mutations on the non-retrying JSON path", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        response(503, { error: "session_reader_unavailable" }, "0.1"),
      );

    await expect(api.deleteSession("session-1")).rejects.toMatchObject({
      status: 503,
    });
    await vi.runAllTimersAsync();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
