import { afterEach, describe, expect, it, vi } from "vitest";

import { emitChatDiagnostic } from "./chatDiagnostics";

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
});
