export type GuiChatLatencyOutcome = "ok" | "error" | "cancelled";

interface GuiChatLatencyEntry {
  elapsed_ms: number;
  outcome?: GuiChatLatencyOutcome;
  stage: string;
  trace_id: string;
}

export interface GuiChatLatencyTrace {
  readonly id: string;
  mark(stage: string, outcome?: GuiChatLatencyOutcome): void;
}

function createTraceId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

function elapsedSince(startedAt: number): number {
  return Math.round((performance.now() - startedAt) * 10) / 10;
}

/**
 * Correlates the browser-visible session-switch stages with backend latency
 * logs. Records contain timings and opaque identifiers only; session titles,
 * message content, auth credentials, and owner identity are never included.
 */
export function startGuiChatLatencyTrace(initialStage: string): GuiChatLatencyTrace {
  const id = createTraceId();
  const startedAt = performance.now();
  const mark = (stage: string, outcome?: GuiChatLatencyOutcome) => {
    const entry: GuiChatLatencyEntry = {
      elapsed_ms: elapsedSince(startedAt),
      stage,
      trace_id: id,
      ...(outcome ? { outcome } : {}),
    };
    console.info("[gui-chat-latency]", entry);
  };

  mark(initialStage);
  return { id, mark };
}
