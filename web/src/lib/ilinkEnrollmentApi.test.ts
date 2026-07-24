// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";

describe("authenticated iLink enrollment API", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.__HERMES_AUTH_REQUIRED__ = true;
    window.__HERMES_BASE_PATH__ = "/hermes";
  });

  it("uses cookie-authenticated owner-scoped endpoints without owner fields", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          attempt_id: "enr_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
          qr_content: "https://example.invalid/qr",
          status: "waiting",
          expires_at: 123,
        }),
        { status: 201, headers: { "Content-Type": "application/json" } },
      ),
    );

    await api.createILinkEnrollment("device-1");

    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/api/auth/ilink/enrollments");
    expect(init).toMatchObject({ method: "POST", credentials: "include" });
    expect(JSON.parse(String(init?.body))).toEqual({
      scene: "internal",
      device_id: "device-1",
    });
  });

  it("encodes opaque attempt identifiers", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ status: "confirmed", expires_at: 123, next_action: "continue_in_wechat" }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    await api.getILinkEnrollment("enr_a/b");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/auth/ilink/enrollments/enr_a%2Fb",
      expect.objectContaining({ credentials: "include" }),
    );
  });
});
