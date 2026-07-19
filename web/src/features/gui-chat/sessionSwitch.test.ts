import { describe, expect, it, vi } from "vitest";

import type { ConnectionState, GatewayEvent } from "@/lib/gatewayClient";
import type { GuiChatConnection } from "./api";
import type { SessionResumeResponse } from "./protocol";
import { GuiChatSessionSwitchCoordinator } from "./sessionSwitch";

class FakeConnection implements GuiChatConnection {
  readonly close = vi.fn();
  readonly createOrResume = vi.fn((_signal?: AbortSignal) => this.result.promise);
  readonly attachImage = vi.fn();
  readonly attachPdf = vi.fn();
  readonly attachFile = vi.fn();
  readonly send = vi.fn();
  readonly stop = vi.fn();
  readonly respondToApproval = vi.fn();
  private readonly eventHandlers = new Set<(event: GatewayEvent) => void>();
  private readonly stateHandlers = new Set<(state: ConnectionState) => void>();
  readonly result = deferred<SessionResumeResponse>();

  readonly client = {
    onEvent: (handler: (event: GatewayEvent) => void) => {
      this.eventHandlers.add(handler);
      return () => this.eventHandlers.delete(handler);
    },
    onState: (handler: (state: ConnectionState) => void) => {
      this.stateHandlers.add(handler);
      handler("idle");
      return () => this.stateHandlers.delete(handler);
    },
  };

  emitEvent(event: GatewayEvent): void {
    for (const handler of this.eventHandlers) handler(event);
  }
}

function createHarness() {
  const commits: Array<{ connection: GuiChatConnection; generation: number; target: string | null }> = [];
  const errors: unknown[] = [];
  const events: GatewayEvent[] = [];
  const coordinator = new GuiChatSessionSwitchCoordinator({
    onCommit: (connection, _response, target, generation) => {
      commits.push({ connection, generation, target });
    },
    onError: (error) => errors.push(error),
    onEvent: (event) => events.push(event),
    onReset: vi.fn(),
    onState: vi.fn(),
  });
  return { commits, coordinator, errors, events };
}

function resumeResponse(sessionId: string, persistedId = sessionId): SessionResumeResponse {
  return {
    session_id: sessionId,
    resumed: persistedId,
    session_key: persistedId,
  };
}

async function flushPromises(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

describe("GuiChatSessionSwitchCoordinator", () => {
  it("starts resume immediately and commits matching buffered events", async () => {
    const connection = new FakeConnection();
    const { commits, coordinator, events } = createHarness();

    const generation = coordinator.start(connection, "parent-session");
    expect(connection.createOrResume).toHaveBeenCalledOnce();
    expect(connection.createOrResume.mock.calls[0]?.[0]).toBeInstanceOf(AbortSignal);

    connection.emitEvent({ type: "message.start", session_id: "runtime-a" });
    connection.emitEvent({ type: "message.start", session_id: "runtime-other" });
    connection.result.resolve(resumeResponse("runtime-a", "canonical-session"));
    await flushPromises();

    expect(commits).toEqual([{ connection, generation, target: "parent-session" }]);
    expect(events).toEqual([{ type: "message.start", session_id: "runtime-a" }]);
  });

  it("keeps the committed connection open until its replacement succeeds", async () => {
    const first = new FakeConnection();
    const second = new FakeConnection();
    const { coordinator } = createHarness();

    coordinator.start(first, "session-a");
    first.result.resolve(resumeResponse("runtime-a"));
    await flushPromises();

    coordinator.start(second, "session-b");
    expect(first.close).not.toHaveBeenCalled();

    second.result.resolve(resumeResponse("runtime-b"));
    await flushPromises();
    expect(first.close).toHaveBeenCalledOnce();
  });

  it("closes the detached committed connection when its replacement fails", async () => {
    const first = new FakeConnection();
    const second = new FakeConnection();
    const { coordinator, errors } = createHarness();

    coordinator.start(first, "session-a");
    first.result.resolve(resumeResponse("runtime-a"));
    await flushPromises();

    coordinator.start(second, "session-b");
    second.result.reject(new Error("replacement failed"));
    await flushPromises();

    expect(first.close).toHaveBeenCalledOnce();
    expect(second.close).toHaveBeenCalledOnce();
    expect(errors).toHaveLength(1);
  });

  it("aborts superseded switches and only commits the latest response", async () => {
    const a = new FakeConnection();
    const b = new FakeConnection();
    const c = new FakeConnection();
    const { commits, coordinator, errors } = createHarness();

    coordinator.start(a, "a");
    const aSignal = a.createOrResume.mock.calls[0]?.[0];
    coordinator.start(b, "b");
    const bSignal = b.createOrResume.mock.calls[0]?.[0];
    coordinator.start(c, "c");

    expect(aSignal?.aborted).toBe(true);
    expect(bSignal?.aborted).toBe(true);
    expect(a.close).toHaveBeenCalledOnce();
    expect(b.close).toHaveBeenCalledOnce();

    a.result.resolve(resumeResponse("runtime-a"));
    b.result.reject(new Error("stale failure"));
    c.result.resolve(resumeResponse("runtime-c"));
    await flushPromises();

    expect(commits).toHaveLength(1);
    expect(commits[0]?.target).toBe("c");
    expect(errors).toEqual([]);
  });

  it("drops events from stale and non-matching runtime sessions", async () => {
    const first = new FakeConnection();
    const second = new FakeConnection();
    const { coordinator, events } = createHarness();

    coordinator.start(first, "a");
    first.result.resolve(resumeResponse("runtime-a"));
    await flushPromises();
    first.emitEvent({ type: "message.start", session_id: "runtime-a" });

    coordinator.start(second, "b");
    first.emitEvent({ type: "message.delta", session_id: "runtime-a" });
    second.result.resolve(resumeResponse("runtime-b"));
    await flushPromises();
    second.emitEvent({ type: "message.start", session_id: "runtime-other" });
    second.emitEvent({ type: "message.start", session_id: "runtime-b" });

    expect(events).toEqual([
      { type: "message.start", session_id: "runtime-a" },
      { type: "message.start", session_id: "runtime-b" },
    ]);
  });

  it("closes candidate and committed connections on dispose", async () => {
    const committed = new FakeConnection();
    const candidate = new FakeConnection();
    const { coordinator } = createHarness();

    coordinator.start(committed, "a");
    committed.result.resolve(resumeResponse("runtime-a"));
    await flushPromises();
    coordinator.start(candidate, "b");

    coordinator.dispose();

    expect(committed.close).toHaveBeenCalledOnce();
    expect(candidate.close).toHaveBeenCalledOnce();
    expect(candidate.createOrResume.mock.calls[0]?.[0]?.aborted).toBe(true);
  });
});

function deferred<T>(): {
  promise: Promise<T>;
  reject: (reason?: unknown) => void;
  resolve: (value: T) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, reject, resolve };
}
