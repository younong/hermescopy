export type ChatDiagnosticSurface = "gui_gateway" | "gui_history" | "terminal_pty";

export interface ChatDiagnostic {
  event: string;
  surface: ChatDiagnosticSurface;
  connectionId?: string;
  closeCode?: number;
  wasClean?: boolean;
  opened?: boolean;
  clientInitiated?: boolean;
  pendingCount?: number;
  loadedCount?: number;
  renderedCount?: number;
  estimatedBytes?: number;
  durationMs?: number;
  outcome?: "cancelled" | "error" | "ok" | "scheduled";
  retryAttempt?: number;
}

interface ChromePerformanceMemory {
  jsHeapSizeLimit?: number;
  totalJSHeapSize?: number;
  usedJSHeapSize?: number;
}

export function emitChatDiagnostic(value: ChatDiagnostic): void {
  const memory = (performance as Performance & { memory?: ChromePerformanceMemory }).memory;
  console.info("[hermes-chat-diagnostic]", {
    schema: 1,
    ...value,
    ...(memory
      ? {
          heapLimitBytes: memory.jsHeapSizeLimit,
          heapTotalBytes: memory.totalJSHeapSize,
          heapUsedBytes: memory.usedJSHeapSize,
        }
      : {}),
  });
}

export function diagnosticId(prefix: string): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Math.random().toString(36).slice(2)}`;
}
