import { describe, expect, it, vi } from "vitest";

import type { ConnectionState, GatewayEvent } from "@/lib/gatewayClient";
import type { GuiChatConnection } from "./api";
import type { SessionResumeResponse } from "./protocol";
import { GuiChatSessionSwitchCoordinator } from "./sessionSwitch";

class FakeConnection implements GuiChatConnection {
  readonly close = vi.fn();
  readonly ensureConnected = vi.fn().mockResolvedValue(undefined);
  readonly attachOwner = vi.fn().mockResolvedValue(undefined);
  readonly collaboration = {
    archiveGroup: vi.fn(),
    createGroup: vi.fn(),
    getGroup: vi.fn(),
    interruptTarget: vi.fn(),
    listGroups: vi.fn(),
    onEvent: vi.fn(() => () => undefined),
    respondToApproval: vi.fn(),
    submitMessage: vi.fn(),
    updateMembers: vi.fn(),
    uploadAttachment: vi.fn(),
  };
  readonly createOrAttach = vi.fn<GuiChatConnection["createOrAttach"]>();
  readonly attachImage = vi.fn();
  readonly attachPdf = vi.fn();
  readonly attachFile = vi.fn();
  readonly send = vi.fn();
  readonly stop = vi.fn();
  readonly switchModel = vi.fn();
  readonly respondToApproval = vi.fn();
  readonly respondToClarify = vi.fn();
  readonly ping = vi.fn();
  readonly reportFrameQueueDiagnostic = vi.fn();
  private readonly eventHandlers = new Set<(event: GatewayEvent) => void>();
  private readonly stateHandlers = new Set<(state: ConnectionState) => void>();
  readonly results: Array<ReturnType<typeof deferred<SessionResumeResponse>>> = [];

  constructor() {
    this.createOrAttach.mockImplementation(() =>
      this.results.shift()?.promise ?? Promise.reject(new Error("missing result")),
    );
  }

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

  nextResult(): ReturnType<typeof deferred<SessionResumeResponse>> {
    const result = deferred<SessionResumeResponse>();
    this.results.push(result);
    return result;
  }

  emitEvent(event: GatewayEvent): void {
    for (const handler of this.eventHandlers) handler(event);
  }
}

function createHarness() {
  const connection = new FakeConnection();
  const commits: Array<{ connection: GuiChatConnection; generation: number; target: string | null }> = [];
  const errors: Array<{
    committedTarget: string | null;
    error: unknown;
    target: string | null;
  }> = [];
  const events: GatewayEvent[] = [];
  const coordinator = new GuiChatSessionSwitchCoordinator(connection, {
    onCommit: (committedConnection, _response, target, generation) => {
      commits.push({ connection: committedConnection, generation, target });
    },
    onError: (error, target, _generation, committedTarget) =>
      errors.push({ committedTarget, error, target }),
    onEvent: (event) => events.push(event),
    onState: vi.fn(),
  });
  return { commits, connection, coordinator, errors, events };
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
  it("forwards trusted employee selection only for new-session creation", async () => {
    const { connection, coordinator } = createHarness();
    connection.nextResult();

    coordinator.start(null, undefined, { employeeAccountId: "employee-a" });

    expect(connection.createOrAttach).toHaveBeenCalledWith(
      null,
      1,
      expect.any(AbortSignal),
      undefined,
      { employeeAccountId: "employee-a" },
    );
  });

  it("starts attach immediately and commits matching buffered events", async () => {
    const { commits, connection, coordinator, events } = createHarness();
    const result = connection.nextResult();

    const generation = coordinator.start("parent-session");
    expect(connection.createOrAttach).toHaveBeenCalledWith(
      "parent-session",
      generation,
      expect.any(AbortSignal),
      undefined,
      undefined,
    );

    connection.emitEvent({ type: "message.start", session_id: "runtime-a" });
    connection.emitEvent({ type: "message.start", session_id: "runtime-other" });
    result.resolve(resumeResponse("runtime-a", "canonical-session"));
    await flushPromises();

    expect(commits).toEqual([{ connection, generation, target: "parent-session" }]);
    expect(coordinator.committedSessionId).toBe("canonical-session");
    expect(events).toEqual([{ type: "message.start", session_id: "runtime-a" }]);
  });

  it("keeps one socket while replacement is pending and after commit", async () => {
    const { connection, coordinator } = createHarness();
    const first = connection.nextResult();
    coordinator.start("session-a");
    first.resolve(resumeResponse("runtime-a", "session-a"));
    await flushPromises();

    const second = connection.nextResult();
    coordinator.start("session-b");
    expect(connection.close).not.toHaveBeenCalled();

    second.resolve(resumeResponse("runtime-b"));
    await flushPromises();
    expect(connection.close).not.toHaveBeenCalled();
  });

  it("keeps the socket but stops routing the old runtime when replacement fails", async () => {
    const { connection, coordinator, errors, events } = createHarness();
    const first = connection.nextResult();
    coordinator.start("session-a");
    first.resolve(resumeResponse("runtime-a", "session-a"));
    await flushPromises();

    const second = connection.nextResult();
    coordinator.start("session-b");
    second.reject(new Error("replacement failed"));
    await flushPromises();

    connection.emitEvent({ type: "message.delta", session_id: "runtime-a" });
    expect(connection.close).not.toHaveBeenCalled();
    expect(errors).toEqual([
      {
        committedTarget: "session-b",
        error: expect.objectContaining({ message: "replacement failed" }),
        target: "session-b",
      },
    ]);
    expect(events).toEqual([]);
  });

  it("aborts superseded switches without closing the reusable socket", async () => {
    const { commits, connection, coordinator, errors } = createHarness();
    const a = connection.nextResult();
    coordinator.start("a");
    const aSignal = connection.createOrAttach.mock.calls[0]?.[2];

    const b = connection.nextResult();
    coordinator.start("b");
    const bSignal = connection.createOrAttach.mock.calls[1]?.[2];

    const c = connection.nextResult();
    coordinator.start("c");

    expect(aSignal?.aborted).toBe(true);
    expect(bSignal?.aborted).toBe(true);
    expect(connection.close).not.toHaveBeenCalled();

    a.resolve(resumeResponse("runtime-a"));
    b.reject(new Error("stale failure"));
    c.resolve(resumeResponse("runtime-c"));
    await flushPromises();

    expect(commits).toHaveLength(1);
    expect(commits[0]?.target).toBe("c");
    expect(errors).toEqual([]);
  });

  it("drops gateway-scoped, stale, and non-matching runtime events", async () => {
    const { connection, coordinator, events } = createHarness();
    const first = connection.nextResult();
    coordinator.start("a");
    connection.emitEvent({ type: "gateway.ready" });
    first.resolve(resumeResponse("runtime-a"));
    await flushPromises();
    connection.emitEvent({ type: "message.start", session_id: "runtime-a" });

    const second = connection.nextResult();
    coordinator.start("b");
    connection.emitEvent({ type: "message.delta", session_id: "runtime-a" });
    second.resolve(resumeResponse("runtime-b"));
    await flushPromises();
    connection.emitEvent({ type: "message.start", session_id: "runtime-other" });
    connection.emitEvent({ type: "message.start", session_id: "runtime-b" });

    expect(events).toEqual([
      { type: "message.start", session_id: "runtime-a" },
      { type: "message.start", session_id: "runtime-b" },
    ]);
  });

  it("can start after cancellation without disposing the reusable connection", async () => {
    const { commits, connection, coordinator } = createHarness();
    const stale = connection.nextResult();
    coordinator.start("stale");
    const staleSignal = connection.createOrAttach.mock.calls[0]?.[2];

    coordinator.cancel();
    expect(staleSignal?.aborted).toBe(true);
    expect(connection.close).not.toHaveBeenCalled();

    const current = connection.nextResult();
    coordinator.start("current");
    stale.resolve(resumeResponse("runtime-stale"));
    current.resolve(resumeResponse("runtime-current"));
    await flushPromises();

    expect(commits).toHaveLength(1);
    expect(commits[0]?.target).toBe("current");
    expect(connection.close).not.toHaveBeenCalled();
  });

  it("closes the reusable connection only on dispose", async () => {
    const { connection, coordinator } = createHarness();
    connection.nextResult();
    coordinator.start("a");
    const signal = connection.createOrAttach.mock.calls[0]?.[2];

    coordinator.dispose();

    expect(connection.close).toHaveBeenCalledOnce();
    expect(signal?.aborted).toBe(true);
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
