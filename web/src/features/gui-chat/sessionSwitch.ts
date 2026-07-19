import type { ConnectionState, GatewayEvent } from "@/lib/gatewayClient";
import type { GuiChatConnection } from "./api";
import type { SessionCreateResponse, SessionResumeResponse } from "./protocol";

export type GuiChatSessionResponse = SessionCreateResponse | SessionResumeResponse;

export interface SessionSwitchCallbacks {
  onCommit(
    connection: GuiChatConnection,
    response: GuiChatSessionResponse,
    targetSessionId: string | null,
    generation: number,
  ): void;
  onError(error: unknown, targetSessionId: string | null, generation: number): void;
  onEvent(event: GatewayEvent, generation: number): void;
  onEventObserved?(event: GatewayEvent, generation: number): void;
  onReset(): void;
  onState(state: ConnectionState): void;
}

interface SessionSwitchCandidate {
  abortController: AbortController;
  connection: GuiChatConnection;
  generation: number;
  offEvents: () => void;
  offState: () => void;
  pendingEvents: GatewayEvent[];
  runtimeSessionId: string | null;
}

interface CommittedConnection {
  connection: GuiChatConnection;
  offEvents: () => void;
  offState: () => void;
}

export class GuiChatSessionSwitchCoordinator {
  private candidate: SessionSwitchCandidate | null = null;
  private committed: CommittedConnection | null = null;
  private generation = 0;
  private readonly callbacks: SessionSwitchCallbacks;

  constructor(callbacks: SessionSwitchCallbacks) {
    this.callbacks = callbacks;
  }

  get currentGeneration(): number {
    return this.generation;
  }

  start(connection: GuiChatConnection, targetSessionId: string | null): number {
    const generation = ++this.generation;
    this.cancelCandidate();
    this.detachCommittedListeners();
    this.callbacks.onReset();

    const candidate: SessionSwitchCandidate = {
      abortController: new AbortController(),
      connection,
      generation,
      offEvents: () => undefined,
      offState: () => undefined,
      pendingEvents: [],
      runtimeSessionId: null,
    };
    candidate.offState = connection.client.onState((state) => {
      if (this.isActive(candidate)) this.callbacks.onState(state);
    });
    candidate.offEvents = connection.client.onEvent((event) => {
      if (!this.isActive(candidate)) return;
      this.callbacks.onEventObserved?.(event, candidate.generation);
      if (candidate.runtimeSessionId === null) {
        candidate.pendingEvents.push(event);
        return;
      }
      if (eventMatchesSession(event, candidate.runtimeSessionId)) {
        this.callbacks.onEvent(event, candidate.generation);
      }
    });
    this.candidate = candidate;

    void connection
      .createOrResume(candidate.abortController.signal)
      .then((response) => this.commit(candidate, response, targetSessionId))
      .catch((error: unknown) => this.fail(candidate, error, targetSessionId));
    return generation;
  }

  cancel(): void {
    this.generation += 1;
    this.cancelCandidate();
  }

  dispose(): void {
    this.cancel();
    this.closeCommitted();
  }

  isGenerationCurrent(generation: number): boolean {
    return generation === this.generation;
  }

  private commit(
    candidate: SessionSwitchCandidate,
    response: GuiChatSessionResponse,
    targetSessionId: string | null,
  ): void {
    if (!this.isCurrent(candidate)) {
      this.closeCandidate(candidate);
      return;
    }

    candidate.runtimeSessionId = response.session_id;
    this.callbacks.onCommit(
      candidate.connection,
      response,
      targetSessionId,
      candidate.generation,
    );
    for (const event of candidate.pendingEvents) {
      if (eventMatchesSession(event, candidate.runtimeSessionId)) {
        this.callbacks.onEvent(event, candidate.generation);
      }
    }
    candidate.pendingEvents = [];

    this.closeCommitted();
    this.committed = {
      connection: candidate.connection,
      offEvents: candidate.offEvents,
      offState: candidate.offState,
    };
    this.candidate = null;
  }

  private fail(
    candidate: SessionSwitchCandidate,
    error: unknown,
    targetSessionId: string | null,
  ): void {
    if (!this.isCurrent(candidate)) return;
    this.closeCandidate(candidate);
    this.candidate = null;
    this.closeCommitted();
    if (!isAbortError(error)) {
      this.callbacks.onError(error, targetSessionId, candidate.generation);
    }
  }

  private isCurrent(candidate: SessionSwitchCandidate): boolean {
    return this.candidate === candidate && candidate.generation === this.generation;
  }

  private isActive(candidate: SessionSwitchCandidate): boolean {
    return candidate.generation === this.generation && (
      this.candidate === candidate || this.committed?.connection === candidate.connection
    );
  }

  private cancelCandidate(): void {
    const candidate = this.candidate;
    if (!candidate) return;
    this.candidate = null;
    candidate.abortController.abort();
    this.closeCandidate(candidate);
  }

  private closeCandidate(candidate: SessionSwitchCandidate): void {
    candidate.offState();
    candidate.offEvents();
    candidate.pendingEvents = [];
    candidate.connection.close();
  }

  private detachCommittedListeners(): void {
    this.committed?.offState();
    this.committed?.offEvents();
  }

  private closeCommitted(): void {
    const committed = this.committed;
    if (!committed) return;
    this.committed = null;
    committed.offState();
    committed.offEvents();
    committed.connection.close();
  }
}

function eventMatchesSession(event: GatewayEvent, runtimeSessionId: string): boolean {
  return !event.session_id || event.session_id === runtimeSessionId;
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}
