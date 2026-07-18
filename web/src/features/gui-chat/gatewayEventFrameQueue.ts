import type { GatewayEvent } from "@/lib/gatewayClient";

type DispatchGatewayEvent = (event: GatewayEvent) => void;
type RequestFrame = (callback: FrameRequestCallback) => number;
type CancelFrame = (handle: number) => void;

export interface GatewayEventFrameQueue {
  enqueue: (event: GatewayEvent) => void;
  reset: () => void;
}

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
    const streamFrame = isBufferedStreamEvent(pendingEvents[0]);
    let frameEnd = 0;
    while (
      frameEnd < pendingEvents.length &&
      isBufferedStreamEvent(pendingEvents[frameEnd]) === streamFrame
    ) {
      frameEnd += 1;
    }

    const frameEvents = pendingEvents.splice(0, frameEnd);
    const events = streamFrame ? mergeBufferedStreamEvents(frameEvents) : frameEvents;
    for (const event of events) {
      dispatch(event);
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

function mergeBufferedStreamEvents(events: GatewayEvent[]): GatewayEvent[] {
  const merged: GatewayEvent[] = [];
  let pending: GatewayEvent | undefined;

  const flush = () => {
    if (pending) {
      merged.push(pending);
      pending = undefined;
    }
  };

  for (const event of events) {
    if (!isBufferedStreamEvent(event)) {
      flush();
      merged.push(event);
      continue;
    }

    if (!pending || pending.type !== event.type || pending.session_id !== event.session_id) {
      flush();
      pending = event;
      continue;
    }

    pending = mergeStreamEventPayload(pending, event);
  }

  flush();
  return merged;
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
