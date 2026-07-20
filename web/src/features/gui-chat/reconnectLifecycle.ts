import type { ConnectionState } from "@/lib/gatewayClient";

const DEFAULT_HEARTBEAT_INTERVAL_MS = 45_000;
const MAX_RECONNECT_DELAY_MS = 15_000;
const RECONNECT_JITTER = 0.2;

interface ReconnectLifecycleOptions {
  close(): void;
  heartbeatIntervalMs?: number;
  ping(): Promise<void>;
  random?: () => number;
  reconnect(): number;
}

export class GuiChatReconnectLifecycle {
  private connectionState: ConnectionState = "idle";
  private disposed = false;
  private heartbeatTimer: ReturnType<typeof setTimeout> | null = null;
  private pingInFlight = false;
  private reconnectAttempt = 0;
  private reconnectGeneration: number | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly options: Required<Pick<ReconnectLifecycleOptions, "heartbeatIntervalMs" | "random">> &
    Omit<ReconnectLifecycleOptions, "heartbeatIntervalMs" | "random">;

  constructor(options: ReconnectLifecycleOptions) {
    this.options = {
      ...options,
      heartbeatIntervalMs: options.heartbeatIntervalMs ?? DEFAULT_HEARTBEAT_INTERVAL_MS,
      random: options.random ?? Math.random,
    };
    window.addEventListener("online", this.handleWake);
    window.addEventListener("pageshow", this.handleWake);
    document.addEventListener("visibilitychange", this.handleVisibilityChange);
  }

  onConnectionState(state: ConnectionState): void {
    if (this.disposed) return;
    this.connectionState = state;
    if (state === "open") {
      this.clearReconnectTimer();
      this.scheduleHeartbeat();
      return;
    }

    this.clearHeartbeatTimer();
    if (state === "closed" || state === "error") this.scheduleReconnect();
  }

  onSwitchSettled(generation: number, committed: boolean): void {
    if (this.disposed) return;
    if (committed) {
      this.reconnectGeneration = null;
      this.reconnectAttempt = 0;
      this.clearReconnectTimer();
      if (this.connectionState === "open") this.scheduleHeartbeat();
      return;
    }
    if (generation !== this.reconnectGeneration) return;
    this.reconnectGeneration = null;
    if (this.connectionState === "closed" || this.connectionState === "error") {
      this.scheduleReconnect();
    }
  }

  cancelRecovery(): void {
    this.reconnectGeneration = null;
    this.reconnectAttempt = 0;
    this.clearReconnectTimer();
  }

  retryNow(): void {
    if (this.disposed || this.reconnectGeneration !== null || this.connectionState === "connecting") {
      return;
    }
    this.clearReconnectTimer();
    this.startReconnect(true);
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    window.removeEventListener("online", this.handleWake);
    window.removeEventListener("pageshow", this.handleWake);
    document.removeEventListener("visibilitychange", this.handleVisibilityChange);
    this.clearHeartbeatTimer();
    this.clearReconnectTimer();
    this.reconnectGeneration = null;
  }

  private readonly handleWake = (): void => {
    if (this.disposed) return;
    if (this.connectionState === "open") {
      void this.runPing();
      return;
    }
    if (this.connectionState === "closed" || this.connectionState === "error") {
      this.clearReconnectTimer();
      this.startReconnect();
    }
  };

  private readonly handleVisibilityChange = (): void => {
    if (document.visibilityState === "visible") this.handleWake();
  };

  private scheduleReconnect(): void {
    if (
      this.disposed ||
      this.reconnectTimer !== null ||
      this.reconnectGeneration !== null ||
      (this.connectionState !== "closed" && this.connectionState !== "error")
    ) {
      return;
    }

    const baseDelay = Math.min(
      MAX_RECONNECT_DELAY_MS,
      1_000 * 2 ** Math.min(this.reconnectAttempt, 4),
    );
    const jitter = 1 + (this.options.random() * 2 - 1) * RECONNECT_JITTER;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.startReconnect();
    }, Math.round(baseDelay * jitter));
  }

  private startReconnect(force = false): void {
    if (
      this.disposed ||
      this.reconnectGeneration !== null ||
      this.connectionState === "connecting" ||
      (!force && this.connectionState !== "closed" && this.connectionState !== "error")
    ) {
      return;
    }

    this.reconnectAttempt += 1;
    try {
      this.reconnectGeneration = this.options.reconnect();
    } catch {
      this.reconnectGeneration = null;
      this.scheduleReconnect();
    }
  }

  private scheduleHeartbeat(): void {
    this.clearHeartbeatTimer();
    if (this.disposed || this.connectionState !== "open") return;
    this.heartbeatTimer = setTimeout(() => {
      this.heartbeatTimer = null;
      void this.runPing();
    }, this.options.heartbeatIntervalMs);
  }

  private async runPing(): Promise<void> {
    if (this.disposed || this.connectionState !== "open" || this.pingInFlight) return;
    this.pingInFlight = true;
    try {
      await this.options.ping();
      if (!this.disposed && this.connectionState === "open") this.scheduleHeartbeat();
    } catch {
      if (!this.disposed) this.options.close();
    } finally {
      this.pingInFlight = false;
    }
  }

  private clearHeartbeatTimer(): void {
    if (this.heartbeatTimer === null) return;
    clearTimeout(this.heartbeatTimer);
    this.heartbeatTimer = null;
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer === null) return;
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }
}
