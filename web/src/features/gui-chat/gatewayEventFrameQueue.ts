import type { GatewayEvent } from "@/lib/gatewayClient";
import {
  buildGuiFrameQueueDiagnostic,
  type GuiFrameQueueDiagnostic,
  type GuiFrameQueueDiagnosticOutcome,
} from "@/lib/chatDiagnostics";

type DispatchGatewayEvent = (event: GatewayEvent) => void;
type RequestFrame = (callback: FrameRequestCallback) => number;
type CancelFrame = (handle: number) => void;
type MonotonicClock = () => number;
type DiagnosticSink = (summary: GuiFrameQueueDiagnostic) => void;

export interface GatewayEventFrameQueue {
  enqueue: (event: GatewayEvent) => void;
  reset: () => void;
}

export interface GatewayEventFrameQueueOptions {
  now?: MonotonicClock;
  onDiagnostic?: DiagnosticSink;
}

interface ActiveDiagnostic {
  startedAt: number;
  completionEvent?: GatewayEvent;
  inputStreamEvents: number;
  inputGraphemes: number;
  maxQueuedEvents: number;
  maxQueuedGraphemes: number;
  renderFrames: number;
  graphemesConsumed: number;
  graphemesPerFrameMax: number;
  graphemesPerFrameHistogram: number[];
  scheduleDelayMaxMs: number;
  scheduleDelayHistogram: number[];
  longFrames: number;
}

const streamSegmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });
const schedulingDelayBucketsMs = [8, 16, 25, 33, 50, 100, 250, 500, 1000] as const;
const graphemesPerFrameBuckets = [1, 3, 6, 12, 24] as const;

export function createGatewayEventFrameQueue(
  dispatch: DispatchGatewayEvent,
  requestFrame: RequestFrame = requestAnimationFrame,
  cancelFrame: CancelFrame = cancelAnimationFrame,
  options: GatewayEventFrameQueueOptions = {},
): GatewayEventFrameQueue {
  const now = options.now ?? (() => performance.now());
  let pendingEvents: GatewayEvent[] = [];
  let pendingGraphemes = 0;
  let frameHandle: number | undefined;
  let frameRequestedAt: number | undefined;
  let diagnostic: ActiveDiagnostic | undefined;

  const finishDiagnostic = (outcome: GuiFrameQueueDiagnosticOutcome) => {
    const current = diagnostic;
    diagnostic = undefined;
    if (outcome === "superseded" && current?.completionEvent) {
      dispatch(current.completionEvent);
    }
    if (!current || !options.onDiagnostic) return;
    try {
      options.onDiagnostic(buildGuiFrameQueueDiagnostic({
        duration_ms: Math.max(0, now() - current.startedAt),
        graphemes_consumed: current.graphemesConsumed,
        graphemes_per_frame_max: current.graphemesPerFrameMax,
        graphemes_per_frame_p95: fixedHistogramPercentile(
          current.graphemesPerFrameHistogram,
          graphemesPerFrameBuckets,
          current.graphemesPerFrameMax,
        ),
        input_graphemes: current.inputGraphemes,
        input_stream_events: current.inputStreamEvents,
        long_frames: current.longFrames,
        max_queued_events: current.maxQueuedEvents,
        max_queued_graphemes: current.maxQueuedGraphemes,
        outcome,
        render_frames: current.renderFrames,
        schedule_delay_max_ms: current.scheduleDelayMaxMs,
        schedule_delay_p95_ms: fixedHistogramPercentile(
          current.scheduleDelayHistogram,
          schedulingDelayBucketsMs,
          current.scheduleDelayMaxMs,
        ),
      }));
    } catch {
      // Diagnostics must never interrupt event delivery or rendering.
    }
  };

  const startDiagnostic = () => {
    if (diagnostic) {
      finishDiagnostic("superseded");
      if (frameHandle !== undefined) {
        cancelFrame(frameHandle);
        frameHandle = undefined;
        frameRequestedAt = undefined;
      }
      pendingEvents = [];
      pendingGraphemes = 0;
    }
    diagnostic = {
      graphemesConsumed: 0,
      graphemesPerFrameHistogram: fixedHistogram(graphemesPerFrameBuckets),
      graphemesPerFrameMax: 0,
      inputGraphemes: 0,
      inputStreamEvents: 0,
      longFrames: 0,
      maxQueuedEvents: pendingEvents.length,
      maxQueuedGraphemes: pendingGraphemes,
      renderFrames: 0,
      scheduleDelayHistogram: fixedHistogram(schedulingDelayBucketsMs),
      scheduleDelayMaxMs: 0,
      startedAt: now(),
    };
  };

  const dispatchTracked = (event: GatewayEvent) => {
    dispatch(event);
    if (event.type === "message.complete") finishDiagnostic("completed");
  };

  const scheduleFlush = () => {
    if (frameHandle === undefined) {
      frameRequestedAt = now();
      frameHandle = requestFrame(flushFrame);
    }
  };

  const flushFrame = () => {
    frameHandle = undefined;
    const observedAt = now();
    const requestedAt = frameRequestedAt;
    frameRequestedAt = undefined;
    const currentDiagnostic = diagnostic;
    if (currentDiagnostic) {
      const delayMs = Math.max(0, observedAt - (requestedAt ?? observedAt));
      currentDiagnostic.renderFrames += 1;
      currentDiagnostic.scheduleDelayMaxMs = Math.max(currentDiagnostic.scheduleDelayMaxMs, delayMs);
      fixedHistogramObserve(currentDiagnostic.scheduleDelayHistogram, schedulingDelayBucketsMs, delayMs);
      if (delayMs > 50) currentDiagnostic.longFrames += 1;
    }

    const first = pendingEvents[0];
    if (!first) return;

    let consumedGraphemes = 0;
    if (isBufferedStreamEvent(first)) {
      const streamEnd = findStreamGroupEnd(pendingEvents);
      const frameEvents = pendingEvents.splice(0, streamEnd);
      const graphemeCount = countStreamGraphemes(frameEvents);
      const takeCount = streamChunkSize(graphemeCount);
      const { event, remaining, taken } = takeStreamChunk(frameEvents, takeCount);
      consumedGraphemes = taken;
      pendingGraphemes = Math.max(0, pendingGraphemes - taken);
      pendingEvents.unshift(...remaining);
      if (event) dispatchTracked(event);
    } else {
      let frameEnd = 1;
      while (frameEnd < pendingEvents.length && !isBufferedStreamEvent(pendingEvents[frameEnd])) frameEnd += 1;
      const events = pendingEvents.splice(0, frameEnd);
      for (const event of events) dispatchTracked(event);
    }

    if (currentDiagnostic && diagnostic === currentDiagnostic) {
      currentDiagnostic.graphemesConsumed += consumedGraphemes;
      currentDiagnostic.graphemesPerFrameMax = Math.max(currentDiagnostic.graphemesPerFrameMax, consumedGraphemes);
      if (consumedGraphemes > 0) {
        fixedHistogramObserve(
          currentDiagnostic.graphemesPerFrameHistogram,
          graphemesPerFrameBuckets,
          consumedGraphemes,
        );
      }
    }

    if (pendingEvents.length > 0) scheduleFlush();
  };

  return {
    enqueue(event) {
      if (event.type === "message.start") startDiagnostic();
      if (event.type === "message.complete" && diagnostic) {
        diagnostic.completionEvent = event;
      }

      if (diagnostic && isBufferedStreamEvent(event)) {
        const graphemes = countEventGraphemes(event);
        diagnostic.inputStreamEvents += 1;
        diagnostic.inputGraphemes += graphemes;
      }

      if (!isBufferedStreamEvent(event) && pendingEvents.length === 0 && frameHandle === undefined) {
        dispatchTracked(event);
        return;
      }

      pendingEvents.push(event);
      if (isBufferedStreamEvent(event)) pendingGraphemes += countEventGraphemes(event);
      if (diagnostic) {
        diagnostic.maxQueuedEvents = Math.max(diagnostic.maxQueuedEvents, pendingEvents.length);
        diagnostic.maxQueuedGraphemes = Math.max(diagnostic.maxQueuedGraphemes, pendingGraphemes);
      }
      scheduleFlush();
    },
    reset() {
      if (frameHandle !== undefined) {
        cancelFrame(frameHandle);
        frameHandle = undefined;
        frameRequestedAt = undefined;
      }
      pendingEvents = [];
      pendingGraphemes = 0;
      finishDiagnostic("cancelled");
    },
  };
}

function fixedHistogram(bounds: readonly number[]): number[] {
  return new Array(bounds.length + 1).fill(0);
}

function fixedHistogramObserve(counts: number[], bounds: readonly number[], value: number): void {
  if (!Number.isFinite(value) || value < 0) return;
  const index = bounds.findIndex((upper) => value <= upper);
  counts[index === -1 ? counts.length - 1 : index] += 1;
}

function fixedHistogramPercentile(
  counts: readonly number[],
  bounds: readonly number[],
  maximum: number,
): number {
  const total = counts.reduce((sum, count) => sum + Math.max(0, count), 0);
  if (total === 0) return 0;
  const rank = Math.max(1, Math.ceil(total * 0.95));
  let cumulative = 0;
  for (let index = 0; index < counts.length; index += 1) {
    cumulative += Math.max(0, counts[index] ?? 0);
    if (cumulative >= rank) return bounds[index] ?? Math.max(0, maximum);
  }
  return Math.max(0, maximum);
}

function isBufferedStreamEvent(event: GatewayEvent | undefined): boolean {
  return event?.type === "message.delta" || event?.type === "thinking.delta" || event?.type === "reasoning.delta";
}

function findStreamGroupEnd(events: GatewayEvent[]): number {
  const first = events[0];
  let end = 0;
  while (end < events.length && events[end]?.type === first?.type && events[end]?.session_id === first?.session_id) end += 1;
  return end;
}

function countEventGraphemes(event: GatewayEvent): number {
  const text = streamText(event);
  return text ? Array.from(streamSegmenter.segment(text)).length : 0;
}

function countStreamGraphemes(events: GatewayEvent[]): number {
  let count = 0;
  for (const event of events) count += countEventGraphemes(event);
  return count;
}

function streamChunkSize(backlog: number): number {
  if (backlog > 240) return 24;
  if (backlog > 120) return 12;
  if (backlog > 48) return 6;
  if (backlog > 16) return 3;
  return 1;
}

function takeStreamChunk(
  events: GatewayEvent[],
  maxGraphemes: number,
): { event: GatewayEvent | undefined; remaining: GatewayEvent[]; taken: number } {
  let consumed = 0;
  let budget = maxGraphemes;
  let merged: GatewayEvent | undefined;

  while (consumed < events.length && budget > 0) {
    const current = events[consumed];
    if (!current) break;
    const text = streamText(current);
    if (!text) {
      merged = merged ? mergeStreamEventPayload(merged, current) : current;
      consumed += 1;
      continue;
    }

    const { head, tail, taken } = splitGraphemes(text, budget);
    if (!head) break;
    const chunk = withStreamText(current, head);
    merged = merged ? mergeStreamEventPayload(merged, chunk) : chunk;
    budget -= taken;
    if (tail) {
      return {
        event: merged,
        remaining: [withStreamText(current, tail), ...events.slice(consumed + 1)],
        taken: maxGraphemes - budget,
      };
    }
    consumed += 1;
  }

  return { event: merged, remaining: events.slice(consumed), taken: maxGraphemes - budget };
}

function splitGraphemes(
  text: string,
  maxGraphemes: number,
): { head: string; tail: string; taken: number } {
  let end = 0;
  let taken = 0;
  for (const segment of streamSegmenter.segment(text)) {
    if (taken >= maxGraphemes) break;
    end = segment.index + segment.segment.length;
    taken += 1;
  }
  return { head: text.slice(0, end), tail: text.slice(end), taken };
}

function streamText(event: GatewayEvent): string | undefined {
  const payload = isRecord(event.payload) ? event.payload : undefined;
  if (!payload) return undefined;
  if (typeof payload.text === "string") return payload.text;
  return typeof payload.rendered === "string" ? payload.rendered : undefined;
}

function withStreamText(event: GatewayEvent, text: string): GatewayEvent {
  const payload = isRecord(event.payload) ? event.payload : {};
  if (typeof payload.text === "string") return { ...event, payload: { ...payload, text } };
  return { ...event, payload: { ...payload, rendered: text } };
}

function mergeStreamEventPayload(base: GatewayEvent, next: GatewayEvent): GatewayEvent {
  const basePayload = isRecord(base.payload) ? base.payload : undefined;
  const nextPayload = isRecord(next.payload) ? next.payload : undefined;
  if (!basePayload || !nextPayload) return next;

  const payload: Record<string, unknown> = { ...basePayload, ...nextPayload };
  for (const field of ["text", "rendered"] as const) {
    const baseText = basePayload[field];
    const nextText = nextPayload[field];
    if (typeof baseText === "string" || typeof nextText === "string") {
      payload[field] = `${typeof baseText === "string" ? baseText : ""}${typeof nextText === "string" ? nextText : ""}`;
    }
  }
  return { ...next, payload };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
