// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getWsTicket } from "./api";

beforeEach(() => {
  window.__HERMES_AUTH_REQUIRED__ = true;
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  delete window.__HERMES_AUTH_REQUIRED__;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("getWsTicket", () => {
  it("mints a fresh single-use ticket for every reconnect", async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ticket: "first", ttl_seconds: 30 }), { status: 200 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ticket: "second", ttl_seconds: 30 }), { status: 200 }),
      );

    await expect(getWsTicket("/api/ws")).resolves.toEqual({
      ticket: "first",
      ttl_seconds: 30,
    });
    await expect(getWsTicket("/api/ws")).resolves.toEqual({
      ticket: "second",
      ttl_seconds: 30,
    });

    expect(fetch).toHaveBeenCalledTimes(2);
    expect(vi.mocked(fetch).mock.calls[0]?.[1]).toMatchObject({
      credentials: "include",
      method: "POST",
    });
  });

  it("runs the existing structured auth recovery before surfacing a 401", async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response(
        JSON.stringify({ error: "session_expired", login_url: "/login?next=%2Fapi%2Fws" }),
        { status: 401 },
      ),
    );
    window.sessionStorage.removeItem("hermes.lastLocation");

    void getWsTicket("/api/ws");

    await vi.waitFor(() => {
      expect(window.sessionStorage.getItem("hermes.lastLocation")).toBe(
        window.location.pathname + window.location.search,
      );
    });
  });
});
