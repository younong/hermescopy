import type { ConnectionState, GatewayEvent } from "@/lib/gatewayClient";
import type { GuiChatConnection, GuiChatSwitchTiming } from "./api";
import type { SessionCreateResponse, SessionResumeResponse } from "./protocol";

export type GuiChatSessionResponse = SessionCreateResponse | SessionResumeResponse;

export interface SessionSwitchCallbacks {
  onCommit(
    connection: GuiChatConnection,
    response: GuiChatSessionResponse,
    targetSessionId: string | null,
    generation: number,
  ): void;
  onError(
    error: unknown,
    targetSessionId: string | null,
    generation: number,
    committedTargetSessionId: string | null,
  ): void;
  onEvent(event: GatewayEvent, generation: number): void;
  onEventObserved?(event: GatewayEvent, generation: number): void;
  onState(state: ConnectionState): void;
}

interface PendingSwitch {
  abortController: AbortController;
  generation: number;
  pendingEvents: GatewayEvent[];
  runtimeSessionId: string | null;
  targetSessionId: string | null;
}

export class GuiChatSessionSwitchCoordinator {
  private readonly connection: GuiChatConnection;
  private readonly callbacks: SessionSwitchCallbacks;
  private readonly offEvents: () => void;
  private readonly offState: () => void;
  private pending: PendingSwitch | null = null;
  private generation = 0;
  private committedRuntimeSessionId: string | null = null;
  private committedTargetSessionId: string | null = null;
  private disposed = false;

  constructor(connection: GuiChatConnection, callbacks: SessionSwitchCallbacks) {
    this.connection = connection;
    this.callbacks = callbacks;
    this.offState = connection.client.onState((state) => this.callbacks.onState(state));
    this.offEvents = connection.client.onEvent((event) => this.observeEvent(event));
  }

  get currentGeneration(): number {
    return this.generation;
  }

  get committedSessionId(): string | null {
    return this.committedTargetSessionId;
  }

  start(targetSessionId: string | null, timing?: GuiChatSwitchTiming): number {
    const generation = ++this.generation;
    this.cancelPending();

    const pending: PendingSwitch = {
      abortController: new AbortController(),
      generation,
      pendingEvents: [],
      runtimeSessionId: null,
      targetSessionId,
    };
    this.pending = pending;

    void this.connection
      .createOrAttach(targetSessionId, generation, pending.abortController.signal, timing)
      .then((response) => this.commit(pending, response))
      .catch((error: unknown) => this.fail(pending, error));
    return generation;
  }

  cancel(): void {
    this.generation += 1;
    this.cancelPending();
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.cancel();
    this.offState();
    this.offEvents();
    this.connection.close();
  }

  isGenerationCurrent(generation: number): boolean {
    return generation === this.generation;
  }

  private observeEvent(event: GatewayEvent): void {
    if (this.disposed) return;
    if (event.type === "gateway.ready" || event.type === "skin.changed") {
      if (this.pending) {
        this.callbacks.onEventObserved?.(event, this.pending.generation);
      }
      return;
    }

    const pending = this.pending;
    if (pending) {
      this.callbacks.onEventObserved?.(event, pending.generation);
      if (event.session_id === this.committedRuntimeSessionId) {
        this.callbacks.onEvent(event, pending.generation);
        return;
      }
      if (event.session_id) pending.pendingEvents.push(event);
      return;
    }

    if (event.session_id && event.session_id === this.committedRuntimeSessionId) {
      this.callbacks.onEvent(event, this.generation);
    }
  }

  private commit(pending: PendingSwitch, response: GuiChatSessionResponse): void {
    if (!this.isCurrent(pending)) return;

    pending.runtimeSessionId = response.session_id;
    this.committedRuntimeSessionId = response.session_id;
    this.committedTargetSessionId =
      ("resumed" in response ? response.resumed ?? response.session_key : null) ??
      response.stored_session_id ??
      pending.targetSessionId;
    this.pending = null;
    this.callbacks.onCommit(
      this.connection,
      response,
      pending.targetSessionId,
      pending.generation,
    );
    for (const event of pending.pendingEvents) {
      if (event.session_id === response.session_id) {
        this.callbacks.onEvent(event, pending.generation);
      }
    }
    pending.pendingEvents = [];
  }

  private fail(pending: PendingSwitch, error: unknown): void {
    if (!this.isCurrent(pending)) return;
    this.pending = null;
    pending.pendingEvents = [];
    if (!isAbortError(error)) {
      this.callbacks.onError(
        error,
        pending.targetSessionId,
        pending.generation,
        this.committedTargetSessionId,
      );
    }
  }

  private isCurrent(pending: PendingSwitch): boolean {
    return this.pending === pending && pending.generation === this.generation;
  }

  private cancelPending(): void {
    const pending = this.pending;
    if (!pending) return;
    this.pending = null;
    pending.abortController.abort();
    pending.pendingEvents = [];
  }
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
