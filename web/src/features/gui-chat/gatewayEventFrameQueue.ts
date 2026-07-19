import type { GatewayEvent } from "@/lib/gatewayClient";

type DispatchGatewayEvent = (event: GatewayEvent) => void;
type RequestFrame = (callback: FrameRequestCallback) => number;
type CancelFrame = (handle: number) => void;

export interface GatewayEventFrameQueue {
  enqueue: (event: GatewayEvent) => void;
  reset: () => void;
}

const streamSegmenter = new Intl.Segmenter(undefined, { granularity: "grapheme" });

export function createGatewayEventFrameQueue(
  dispatch: DispatchGatewayEvent,
  requestFrame: RequestFrame = requestAnimationFrame,
  cancelFrame: CancelFrame = cancelAnimationFrame,
): GatewayEventFrameQueue {
  let pendingEvents: GatewayEvent[] = [];
  let frameHandle: number | undefined;

  const scheduleFlush = () => {
    if (frameHandle === undefined) {
      frameHandle = requestFrame(flushFrame);
    }
  };

  const flushFrame = () => {
    frameHandle = undefined;
    const first = pendingEvents[0];
    if (!first) return;

    if (isBufferedStreamEvent(first)) {
      const streamEnd = findStreamGroupEnd(pendingEvents);
      const frameEvents = pendingEvents.splice(0, streamEnd);
      const graphemeCount = countStreamGraphemes(frameEvents);
      const takeCount = streamChunkSize(graphemeCount);
      const { event, remaining } = takeStreamChunk(frameEvents, takeCount);
      pendingEvents.unshift(...remaining);
      if (event) dispatch(event);
    } else {
      let frameEnd = 1;
      while (frameEnd < pendingEvents.length && !isBufferedStreamEvent(pendingEvents[frameEnd])) {
        frameEnd += 1;
      }
      const events = pendingEvents.splice(0, frameEnd);
      for (const event of events) dispatch(event);
    }

    if (pendingEvents.length > 0) scheduleFlush();
  };

  return {
    enqueue(event) {
      if (
        !isBufferedStreamEvent(event) &&
        pendingEvents.length === 0 &&
        frameHandle === undefined
      ) {
        dispatch(event);
        return;
      }

      pendingEvents.push(event);
      scheduleFlush();
    },
    reset() {
      if (frameHandle !== undefined) {
        cancelFrame(frameHandle);
        frameHandle = undefined;
      }
      pendingEvents = [];
    },
  };
}

function isBufferedStreamEvent(event: GatewayEvent | undefined): boolean {
  return (
    event?.type === "message.delta" ||
    event?.type === "thinking.delta" ||
    event?.type === "reasoning.delta"
  );
}

function findStreamGroupEnd(events: GatewayEvent[]): number {
  const first = events[0];
  let end = 0;
  while (
    end < events.length &&
    events[end]?.type === first?.type &&
    events[end]?.session_id === first?.session_id
  ) {
    end += 1;
  }
  return end;
}

function countStreamGraphemes(events: GatewayEvent[]): number {
  let count = 0;
  for (const event of events) {
    const text = streamText(event);
    if (text) count += Array.from(streamSegmenter.segment(text)).length;
  }
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
): { event: GatewayEvent | undefined; remaining: GatewayEvent[] } {
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
      };
    }
    consumed += 1;
  }

  return { event: merged, remaining: events.slice(consumed) };
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
      payload[field] = `${typeof baseText === "string" ? baseText : ""}${
        typeof nextText === "string" ? nextText : ""
      }`;
    }
  }
  return { ...next, payload };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
