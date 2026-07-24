// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from "vitest";
import { createEnrollment, getEnrollment } from "./publicEnrollmentApi";

vi.mock("./api", () => ({ HERMES_BASE_PATH: "/hermes" }));

describe("public enrollment API", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("uses the public endpoint without dashboard credentials or profile headers", async () => {
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

    await createEnrollment("device-1");

    expect(fetchMock).toHaveBeenCalledOnce();
    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe("/hermes/api/public/ilink/enrollments");
    expect(init).toMatchObject({ method: "POST", cache: "no-store" });
    expect(init?.credentials).toBeUndefined();
    expect(new Headers(init?.headers).has("Authorization")).toBe(false);
    expect(new Headers(init?.headers).has("X-Hermes-Profile")).toBe(false);
  });

  it("encodes the opaque attempt id and performs a local status GET", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ status: "confirmed", expires_at: 123, next_action: "continue_in_wechat" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await getEnrollment("enr_a/b");

    expect(fetchMock).toHaveBeenCalledWith(
      "/hermes/api/public/ilink/enrollments/enr_a%2Fb",
      expect.objectContaining({ cache: "no-store" }),
    );
  });
});
