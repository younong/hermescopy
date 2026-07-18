import { describe, expect, it, vi } from "vitest";

import type { GatewayEvent } from "@/lib/gatewayClient";
import { guiChatReducer } from "./reducer";
import { createGatewayEventFrameQueue } from "./gatewayEventFrameQueue";
import { initialGuiChatState } from "./types";

function createFrameHarness() {
  const callbacks = new Map<number, FrameRequestCallback>();
  let nextHandle = 1;
  const requestFrame = vi.fn((callback: FrameRequestCallback) => {
    const handle = nextHandle++;
    callbacks.set(handle, callback);
    return handle;
  });
  const cancelFrame = vi.fn((handle: number) => {
    callbacks.delete(handle);
  });

  return {
    cancelFrame,
    flushFrame() {
      const entry = callbacks.entries().next().value;
      if (!entry) throw new Error("expected a queued animation frame");
      const [handle, callback] = entry;
      callbacks.delete(handle);
      callback(0);
    },
    pendingFrames() {
      return callbacks.size;
    },
    requestFrame,
  };
}

function event(type: string, text?: string): GatewayEvent {
  return {
    payload: text === undefined ? undefined : { text },
    session_id: "sid",
    type,
  };
}

describe("createGatewayEventFrameQueue", () => {
  it("paints streamed deltas before a tightly following completion", () => {
    const frames = createFrameHarness();
    let state = guiChatReducer(initialGuiChatState, {
      response: { messages: [], session_id: "sid" },
      type: "session.created",
    });
    const queue = createGatewayEventFrameQueue(
      (gatewayEvent) => {
        state = guiChatReducer(state, { event: gatewayEvent, type: "event" });
      },
      frames.requestFrame,
      frames.cancelFrame,
    );

    queue.enqueue(event("message.start"));
    queue.enqueue(event("message.delta", "partial "));
    queue.enqueue(event("message.delta", "answer"));
    queue.enqueue({
      payload: { status: "complete", text: "partial answer" },
      session_id: "sid",
      type: "message.complete",
    });

    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({ streaming: true, text: "" });

    frames.flushFrame();
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({ streaming: true, text: "partial answer" });
    expect(state.isGenerating).toBe(true);

    frames.flushFrame();
    expect(state.messages).toHaveLength(1);
    expect(state.messages[0]).toMatchObject({
      status: "complete",
      streaming: false,
      text: "partial answer",
    });
    expect(state.isGenerating).toBe(false);
    expect(frames.pendingFrames()).toBe(0);
  });

  it("preserves order across stream and control event boundaries", () => {
    const frames = createFrameHarness();
    const dispatched: GatewayEvent[] = [];
    const queue = createGatewayEventFrameQueue(
      (gatewayEvent) => dispatched.push(gatewayEvent),
      frames.requestFrame,
      frames.cancelFrame,
    );

    queue.enqueue(event("message.delta", "one"));
    queue.enqueue(event("tool.start"));
    queue.enqueue(event("message.delta", "two"));

    frames.flushFrame();
    expect(dispatched.map(({ type }) => type)).toEqual(["message.delta"]);
    frames.flushFrame();
    expect(dispatched.map(({ type }) => type)).toEqual(["message.delta", "tool.start"]);
    frames.flushFrame();
    expect(dispatched.map(({ type }) => type)).toEqual([
      "message.delta",
      "tool.start",
      "message.delta",
    ]);
  });

  it("dispatches an idle control event without waiting for a frame", () => {
    const frames = createFrameHarness();
    const dispatch = vi.fn();
    const queue = createGatewayEventFrameQueue(
      dispatch,
      frames.requestFrame,
      frames.cancelFrame,
    );
    const controlEvent = event("tool.start");

    queue.enqueue(controlEvent);

    expect(dispatch).toHaveBeenCalledWith(controlEvent);
    expect(frames.requestFrame).not.toHaveBeenCalled();
  });

  it("cancels queued stream and completion events on reset", () => {
    const frames = createFrameHarness();
    const dispatch = vi.fn();
    const queue = createGatewayEventFrameQueue(
      dispatch,
      frames.requestFrame,
      frames.cancelFrame,
    );

    queue.enqueue(event("message.delta", "stale"));
    queue.enqueue(event("message.complete", "stale"));
    queue.reset();

    expect(frames.cancelFrame).toHaveBeenCalledOnce();
    expect(frames.pendingFrames()).toBe(0);
    expect(dispatch).not.toHaveBeenCalled();
  });
});
