export type ChatDiagnosticSurface = "gui_gateway" | "gui_history" | "terminal_pty";

export type GuiFrameQueueDiagnosticOutcome = "cancelled" | "completed" | "superseded";

export interface GuiFrameQueueDiagnostic {
  schema_version: 1;
  outcome: GuiFrameQueueDiagnosticOutcome;
  duration_ms: number;
  input_stream_events: number;
  input_graphemes: number;
  max_queued_events: number;
  max_queued_graphemes: number;
  render_frames: number;
  graphemes_consumed: number;
  graphemes_per_frame_max: number;
  graphemes_per_frame_p95: number;
  schedule_delay_max_ms: number;
  schedule_delay_p95_ms: number;
  long_frames: number;
}

export type GuiFrameQueueDiagnosticInput = Omit<GuiFrameQueueDiagnostic, "schema_version">;

const guiFrameQueueDiagnosticKeys = [
  "duration_ms",
  "graphemes_consumed",
  "graphemes_per_frame_max",
  "graphemes_per_frame_p95",
  "input_graphemes",
  "input_stream_events",
  "long_frames",
  "max_queued_events",
  "max_queued_graphemes",
  "outcome",
  "render_frames",
  "schedule_delay_max_ms",
  "schedule_delay_p95_ms",
] as const;

const guiFrameQueueCountKeys = [
  "graphemes_consumed",
  "input_graphemes",
  "input_stream_events",
  "long_frames",
  "max_queued_events",
  "max_queued_graphemes",
  "render_frames",
] as const;

const guiFrameQueueNumberKeys = [
  "duration_ms",
  "graphemes_per_frame_max",
  "graphemes_per_frame_p95",
  "schedule_delay_max_ms",
  "schedule_delay_p95_ms",
] as const;

const MAX_DIAGNOSTIC_COUNT = 10_000_000;
const MAX_DIAGNOSTIC_MS = 3_600_000;

export function buildGuiFrameQueueDiagnostic(
  value: GuiFrameQueueDiagnosticInput,
): GuiFrameQueueDiagnostic {
  if (!isRecord(value)) throw new TypeError("invalid GUI frame queue diagnostic");
  const keys = Object.keys(value).sort();
  const expected = [...guiFrameQueueDiagnosticKeys].sort();
  if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) {
    throw new TypeError("invalid GUI frame queue diagnostic fields");
  }
  if (!(["cancelled", "completed", "superseded"] as const).includes(value.outcome)) {
    throw new TypeError("invalid GUI frame queue diagnostic outcome");
  }
  for (const key of guiFrameQueueCountKeys) {
    assertBoundedNumber(value[key], key, MAX_DIAGNOSTIC_COUNT, true);
  }
  for (const key of guiFrameQueueNumberKeys) {
    assertBoundedNumber(value[key], key, MAX_DIAGNOSTIC_MS, false);
  }
  return { schema_version: 1, ...value };
}

function assertBoundedNumber(
  value: unknown,
  field: string,
  maximum: number,
  integer: boolean,
): asserts value is number {
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value < 0 ||
    value > maximum ||
    (integer && !Number.isInteger(value))
  ) {
    throw new TypeError(`invalid GUI frame queue diagnostic ${field}`);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

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
