// @vitest-environment jsdom

import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PublicJoinApp, { isPublicJoinPath } from "./PublicJoinApp";

const mocks = vi.hoisted(() => ({
  createEnrollment: vi.fn(),
  getEnrollment: vi.fn(),
  toDataURL: vi.fn(),
}));

vi.mock("@/lib/publicEnrollmentApi", () => ({
  createEnrollment: mocks.createEnrollment,
  getEnrollment: mocks.getEnrollment,
  enrollmentDeviceId: () => "device-1",
}));

vi.mock("qrcode", () => ({
  toDataURL: mocks.toDataURL,
}));

let root: Root | null = null;

beforeEach(() => {
  (globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.useFakeTimers();
  mocks.createEnrollment.mockReset();
  mocks.getEnrollment.mockReset();
  mocks.toDataURL.mockReset();
  document.body.innerHTML = '<div id="root"></div>';
});

afterEach(async () => {
  if (root) await act(async () => root?.unmount());
  root = null;
  vi.useRealTimers();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

describe("PublicJoinApp", () => {
  it("only classifies the exact join entry point as public", () => {
    expect(isPublicJoinPath("/join")).toBe(true);
    expect(isPublicJoinPath("/join/")).toBe(true);
    expect(isPublicJoinPath("/join/admin")).toBe(false);
    expect(isPublicJoinPath("/hermes/join", "/hermes")).toBe(true);
    expect(isPublicJoinPath("/hermes/join/", "/hermes/")).toBe(true);
    expect(isPublicJoinPath("/other/join", "/hermes")).toBe(false);
  });

  it("renders a QR, polls local state, and stops at confirmation", async () => {
    mocks.createEnrollment.mockResolvedValue({
      attempt_id: "enr_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      qr_content: "https://example.invalid/qr?<script>",
      status: "waiting",
      expires_at: 123,
    });
    mocks.toDataURL.mockResolvedValue("data:image/png;base64,qr");
    mocks.getEnrollment.mockResolvedValue({
      status: "confirmed",
      expires_at: 123,
      next_action: "continue_in_wechat",
    });

    await render();
    const image = document.querySelector("img") as HTMLImageElement;
    expect(image.src).toContain("data:image/png;base64,qr");
    expect(document.querySelector("script")).toBeNull();

    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(mocks.getEnrollment).toHaveBeenCalledOnce();
    expect(document.body.textContent).toContain("Connected");
    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(mocks.getEnrollment).toHaveBeenCalledOnce();
  });

  it("creates a replacement attempt only after an explicit retry", async () => {
    mocks.createEnrollment.mockResolvedValue({
      attempt_id: "enr_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      qr_content: "qr",
      status: "waiting",
      expires_at: 123,
    });
    mocks.toDataURL.mockResolvedValue("data:image/png;base64,qr");
    mocks.getEnrollment.mockResolvedValue({ status: "expired", expires_at: 123, next_action: "retry" });

    await render();
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(mocks.createEnrollment).toHaveBeenCalledOnce();
    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(mocks.createEnrollment).toHaveBeenCalledOnce();

    const button = document.querySelector("button") as HTMLButtonElement;
    await act(async () => button.click());
    expect(mocks.createEnrollment).toHaveBeenCalledTimes(2);
  });
});

async function render() {
  root = createRoot(document.getElementById("root")!);
  await act(async () => root?.render(<PublicJoinApp />));
}
