import { afterEach, describe, expect, it, vi } from "vitest";

import { startGuiChatLatencyTrace } from "./latencyTrace";

describe("GUI chat latency trace", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("emits correlated elapsed stages without chat content", () => {
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(
      "cdd27bc1-73df-43eb-a54d-f662ee263c33",
    );
    vi.spyOn(performance, "now").mockReturnValueOnce(10).mockReturnValueOnce(10).mockReturnValueOnce(42.34);
    const info = vi.spyOn(console, "info").mockImplementation(() => undefined);

    const trace = startGuiChatLatencyTrace("session_list.click");
    trace.mark("websocket.open", "ok");

    expect(trace.id).toBe("cdd27bc1-73df-43eb-a54d-f662ee263c33");
    expect(info).toHaveBeenNthCalledWith(1, "[gui-chat-latency]", {
      elapsed_ms: 0,
      stage: "session_list.click",
      trace_id: trace.id,
    });
    expect(info).toHaveBeenNthCalledWith(2, "[gui-chat-latency]", {
      elapsed_ms: 32.3,
      outcome: "ok",
      stage: "websocket.open",
      trace_id: trace.id,
    });
  });
});
