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

  let now = 0;

  return {
    cancelFrame,
    flushFrame() {
      const entry = callbacks.entries().next().value;
      if (!entry) throw new Error("expected a queued animation frame");
      const [handle, callback] = entry;
      callbacks.delete(handle);
      callback(now);
    },
    now: () => now,
    pendingFrames() {
      return callbacks.size;
    },
    requestFrame,
    setNow(value: number) {
      now = value;
    },
  };
}

function event(type: string, text?: string): GatewayEvent {
  return {
    payload: text === undefined ? undefined : { text },
    session_id: "sid",
    type,
  };
}

function eventText(gatewayEvent: GatewayEvent): string | undefined {
  const payload = gatewayEvent.payload;
  if (!payload || typeof payload !== "object") return undefined;
  return "text" in payload && typeof payload.text === "string" ? payload.text : undefined;
}

describe("createGatewayEventFrameQueue", () => {
  it("drains a burst over animation frames before completing", () => {
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
    expect(state.messages[0]).toMatchObject({ streaming: true, text: "p" });
    expect(state.isGenerating).toBe(true);

    for (let index = 0; index < "partial answer".length - 1; index += 1) {
      frames.flushFrame();
    }
    expect(state.messages[0]).toMatchObject({ streaming: true, text: "partial answer" });
    expect(state.isGenerating).toBe(true);

    frames.flushFrame();
    expect(state.messages[0]).toMatchObject({
      status: "complete",
      streaming: false,
      text: "partial answer",
    });
    expect(state.isGenerating).toBe(false);
    expect(frames.pendingFrames()).toBe(0);
  });

  it("accelerates the drain rate as the presentation backlog grows", () => {
    const frames = createFrameHarness();
    const dispatched: GatewayEvent[] = [];
    const queue = createGatewayEventFrameQueue(
      (gatewayEvent) => dispatched.push(gatewayEvent),
      frames.requestFrame,
      frames.cancelFrame,
    );

    queue.enqueue(event("message.delta", "a".repeat(241)));
    frames.flushFrame();
    expect(eventText(dispatched[0])).toHaveLength(24);

    frames.flushFrame();
    expect(eventText(dispatched.at(-1)!)).toHaveLength(12);

    for (let index = 0; index < 8; index += 1) frames.flushFrame();
    expect(eventText(dispatched.at(-1)!)).toHaveLength(12);
    frames.flushFrame();
    expect(eventText(dispatched.at(-1)!)).toHaveLength(6);
  });

  it("does not stall when a streamed status event has empty text", () => {
    const frames = createFrameHarness();
    const dispatched: GatewayEvent[] = [];
    const queue = createGatewayEventFrameQueue(
      (gatewayEvent) => dispatched.push(gatewayEvent),
      frames.requestFrame,
      frames.cancelFrame,
    );

    queue.enqueue(event("thinking.delta", ""));
    queue.enqueue(event("thinking.delta", "done"));
    queue.enqueue(event("message.delta", "answer"));

    frames.flushFrame();
    expect(dispatched).toHaveLength(1);
    expect(dispatched[0]).toMatchObject({ type: "thinking.delta" });
    expect(eventText(dispatched[0])).toBe("d");

    for (let index = 0; index < 3; index += 1) frames.flushFrame();
    expect(eventText(dispatched.at(-1)!)).toBe("e");

    frames.flushFrame();
    expect(dispatched.at(-1)).toMatchObject({ type: "message.delta" });
    expect(eventText(dispatched.at(-1)!)).toBe("a");
  });

  it("does not split emoji or combined Unicode graphemes", () => {
    const frames = createFrameHarness();
    const dispatched: GatewayEvent[] = [];
    const queue = createGatewayEventFrameQueue(
      (gatewayEvent) => dispatched.push(gatewayEvent),
      frames.requestFrame,
      frames.cancelFrame,
    );

    queue.enqueue(event("message.delta", "👨‍👩‍👧‍👦é好"));

    frames.flushFrame();
    frames.flushFrame();
    frames.flushFrame();

    expect(dispatched.map(eventText)).toEqual(["👨‍👩‍👧‍👦", "é", "好"]);
  });

  it("preserves order across stream type and control event boundaries", () => {
    const frames = createFrameHarness();
    const dispatched: GatewayEvent[] = [];
    const queue = createGatewayEventFrameQueue(
      (gatewayEvent) => dispatched.push(gatewayEvent),
      frames.requestFrame,
      frames.cancelFrame,
    );

    queue.enqueue(event("message.delta", "one"));
    queue.enqueue(event("reasoning.delta", "why"));
    queue.enqueue(event("tool.start"));
    queue.enqueue(event("message.delta", "two"));

    for (let index = 0; index < 3; index += 1) frames.flushFrame();
    expect(dispatched.map(({ type }) => type)).toEqual([
      "message.delta",
      "message.delta",
      "message.delta",
    ]);
    frames.flushFrame();
    expect(dispatched.at(-1)).toMatchObject({ type: "reasoning.delta" });
    frames.flushFrame();
    frames.flushFrame();
    frames.flushFrame();
    expect(dispatched.map(({ type }) => type)).toEqual([
      "message.delta",
      "message.delta",
      "message.delta",
      "reasoning.delta",
      "reasoning.delta",
      "reasoning.delta",
      "tool.start",
    ]);
    frames.flushFrame();
    expect(dispatched.at(-1)).toMatchObject({ type: "message.delta" });
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

  it("reports one content-free aggregate only after completion drains", () => {
    const frames = createFrameHarness();
    const diagnostics: unknown[] = [];
    const queue = createGatewayEventFrameQueue(
      vi.fn(),
      frames.requestFrame,
      frames.cancelFrame,
      { now: frames.now, onDiagnostic: (summary) => diagnostics.push(summary) },
    );

    frames.setNow(10);
    queue.enqueue(event("message.start"));
    queue.enqueue(event("message.delta", "👨‍👩‍👧‍👦abc"));
    queue.enqueue(event("message.complete", "ignored-complete-content"));

    frames.setNow(70);
    frames.flushFrame();
    expect(diagnostics).toEqual([]);
    frames.setNow(86);
    frames.flushFrame();
    frames.setNow(102);
    frames.flushFrame();
    frames.setNow(118);
    frames.flushFrame();
    expect(diagnostics).toEqual([]);
    frames.setNow(134);
    frames.flushFrame();

    expect(diagnostics).toEqual([
      {
        duration_ms: 124,
        graphemes_consumed: 4,
        graphemes_per_frame_max: 1,
        graphemes_per_frame_p95: 1,
        input_graphemes: 4,
        input_stream_events: 1,
        long_frames: 1,
        max_queued_events: 2,
        max_queued_graphemes: 4,
        outcome: "completed",
        render_frames: 5,
        schedule_delay_max_ms: 60,
        schedule_delay_p95_ms: 100,
        schema_version: 1,
      },
    ]);
    expect(JSON.stringify(diagnostics)).not.toContain("ignored-complete-content");
  });

  it("drops superseded queued deltas before dispatching the new stream", () => {
    const frames = createFrameHarness();
    const dispatched: GatewayEvent[] = [];
    const queue = createGatewayEventFrameQueue(
      (gatewayEvent) => dispatched.push(gatewayEvent),
      frames.requestFrame,
      frames.cancelFrame,
      { now: frames.now },
    );

    queue.enqueue(event("message.start"));
    queue.enqueue(event("message.delta", "stale"));
    queue.enqueue(event("message.complete", "stale"));
    queue.enqueue(event("message.start"));
    queue.enqueue(event("message.delta", "new"));
    frames.flushFrame();

    expect(dispatched.map(eventText).filter(Boolean)).toEqual(["stale", "n"]);
    expect(dispatched.map(({ type }) => type)).toEqual([
      "message.start",
      "message.complete",
      "message.start",
      "message.delta",
    ]);
  });

  it("reports superseded and cancelled lifecycles without carrying content", () => {
    const frames = createFrameHarness();
    const diagnostics: Array<{ outcome: string }> = [];
    const queue = createGatewayEventFrameQueue(
      vi.fn(),
      frames.requestFrame,
      frames.cancelFrame,
      { now: frames.now, onDiagnostic: (summary) => diagnostics.push(summary) },
    );

    queue.enqueue(event("message.start"));
    queue.enqueue(event("message.delta", "private"));
    frames.setNow(20);
    queue.enqueue(event("message.start"));
    frames.setNow(30);
    queue.reset();

    expect(diagnostics.map(({ outcome }) => outcome)).toEqual(["superseded", "cancelled"]);
    expect(JSON.stringify(diagnostics)).not.toContain("private");
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
