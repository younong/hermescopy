import { afterEach, describe, expect, it, vi } from "vitest";

import { buildGuiFrameQueueDiagnostic, emitChatDiagnostic } from "./chatDiagnostics";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("chat diagnostics", () => {
  it("emits only the structured allowlisted fields supplied by typed callers", () => {
    const info = vi.spyOn(console, "info").mockImplementation(() => undefined);

    emitChatDiagnostic({
      clientInitiated: false,
      closeCode: 1006,
      connectionId: "gui-safe-id",
      event: "closed",
      opened: true,
      pendingCount: 2,
      surface: "gui_gateway",
      wasClean: false,
    });

    expect(info).toHaveBeenCalledTimes(1);
    const [label, payload] = info.mock.calls[0];
    expect(label).toBe("[hermes-chat-diagnostic]");
    expect(payload).toMatchObject({
      clientInitiated: false,
      closeCode: 1006,
      connectionId: "gui-safe-id",
      event: "closed",
      opened: true,
      pendingCount: 2,
      schema: 1,
      surface: "gui_gateway",
      wasClean: false,
    });
    expect(payload).not.toHaveProperty("reason");
    expect(payload).not.toHaveProperty("url");
    expect(payload).not.toHaveProperty("token");
  });

  it("builds an exact server-safe frame queue schema", () => {
    const summary = buildGuiFrameQueueDiagnostic({
      duration_ms: 120,
      graphemes_consumed: 8,
      graphemes_per_frame_max: 3,
      graphemes_per_frame_p95: 3,
      input_graphemes: 8,
      input_stream_events: 2,
      long_frames: 1,
      max_queued_events: 3,
      max_queued_graphemes: 8,
      outcome: "completed",
      render_frames: 4,
      schedule_delay_max_ms: 64,
      schedule_delay_p95_ms: 100,
    });

    expect(summary).toEqual(expect.objectContaining({ schema_version: 1, outcome: "completed" }));
    for (const unsafe of ["connectionId", "session_id", "owner_id", "token", "heapUsedBytes", "text"]) {
      expect(summary).not.toHaveProperty(unsafe);
    }
  });

  it("rejects extra, non-finite, and non-numeric frame queue fields", () => {
    const valid = {
      duration_ms: 120,
      graphemes_consumed: 8,
      graphemes_per_frame_max: 3,
      graphemes_per_frame_p95: 3,
      input_graphemes: 8,
      input_stream_events: 2,
      long_frames: 1,
      max_queued_events: 3,
      max_queued_graphemes: 8,
      outcome: "completed" as const,
      render_frames: 4,
      schedule_delay_max_ms: 64,
      schedule_delay_p95_ms: 100,
    };

    expect(() => buildGuiFrameQueueDiagnostic({ ...valid, text: "secret" } as never)).toThrow();
    expect(() => buildGuiFrameQueueDiagnostic({ ...valid, duration_ms: Number.NaN })).toThrow();
    expect(() => buildGuiFrameQueueDiagnostic({ ...valid, input_stream_events: 1.5 })).toThrow();
  });
});
