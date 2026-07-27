// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WebSocketReconnectLifecycle } from "./reconnectLifecycle";

let lifecycle: WebSocketReconnectLifecycle | null;

beforeEach(() => {
  vi.useFakeTimers();
  lifecycle = null;
});

afterEach(() => {
  lifecycle?.dispose();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function createLifecycle(ping = vi.fn().mockResolvedValue(undefined)) {
  const close = vi.fn();
  let generation = 0;
  const reconnect = vi.fn(() => ++generation);
  lifecycle = new WebSocketReconnectLifecycle({
    close,
    ping,
    random: () => 0.5,
    reconnect,
  });
  return { close, ping, reconnect };
}

describe("WebSocketReconnectLifecycle", () => {
  it("retries with capped exponential backoff and one attempt at a time", () => {
    const { reconnect } = createLifecycle();
    lifecycle?.onConnectionState("closed");
    vi.advanceTimersByTime(999);
    expect(reconnect).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(reconnect).toHaveBeenCalledTimes(1);

    lifecycle?.onReconnectSettled(1, false);
    vi.advanceTimersByTime(1_999);
    expect(reconnect).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1);
    expect(reconnect).toHaveBeenCalledTimes(2);

    lifecycle?.onReconnectSettled(2, false);
    vi.advanceTimersByTime(4_000);
    lifecycle?.onReconnectSettled(3, false);
    vi.advanceTimersByTime(8_000);
    lifecycle?.onReconnectSettled(4, false);
    vi.advanceTimersByTime(15_000);
    lifecycle?.onReconnectSettled(5, false);
    vi.advanceTimersByTime(14_999);
    expect(reconnect).toHaveBeenCalledTimes(5);
    vi.advanceTimersByTime(1);
    expect(reconnect).toHaveBeenCalledTimes(6);
  });

  it("retries when an asynchronous reconnect attempt rejects", async () => {
    const close = vi.fn();
    const reconnect = vi.fn().mockRejectedValue(new Error("ticket unavailable"));
    lifecycle = new WebSocketReconnectLifecycle({
      close,
      ping: vi.fn().mockResolvedValue(undefined),
      random: () => 0.5,
      reconnect,
    });
    lifecycle.onConnectionState("closed");

    await vi.advanceTimersByTimeAsync(1_000);
    expect(reconnect).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1_999);
    expect(reconnect).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1);
    expect(reconnect).toHaveBeenCalledTimes(2);
  });

  it("recovers immediately on online and pageshow without duplicate attempts", () => {
    const { reconnect } = createLifecycle();
    lifecycle?.onConnectionState("closed");

    window.dispatchEvent(new Event("online"));
    window.dispatchEvent(new Event("pageshow"));

    expect(reconnect).toHaveBeenCalledOnce();
    vi.advanceTimersByTime(20_000);
    expect(reconnect).toHaveBeenCalledOnce();
  });

  it("pings while open and closes the stale connection on failure", async () => {
    const ping = vi.fn().mockRejectedValue(new Error("stale"));
    const { close } = createLifecycle(ping);
    lifecycle?.onConnectionState("open");

    await vi.advanceTimersByTimeAsync(45_000);

    expect(ping).toHaveBeenCalledOnce();
    expect(close).toHaveBeenCalledOnce();
  });

  it("pings immediately when a visible open page wakes", async () => {
    const { ping } = createLifecycle();
    lifecycle?.onConnectionState("open");

    window.dispatchEvent(new Event("pageshow"));
    await Promise.resolve();

    expect(ping).toHaveBeenCalledOnce();
  });

  it("cancels timers and listeners on disposal", () => {
    const { reconnect } = createLifecycle();
    lifecycle?.onConnectionState("closed");
    lifecycle?.dispose();
    lifecycle = null;

    vi.advanceTimersByTime(20_000);
    window.dispatchEvent(new Event("online"));

    expect(reconnect).not.toHaveBeenCalled();
  });
});
