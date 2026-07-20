import { describe, expect, it } from "vitest";

import { JsonRpcGatewayClient, JsonRpcGatewayError, type WebSocketLike } from "./json-rpc-gateway";

class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 3;

  readyState = FakeWebSocket.CONNECTING;
  sent: string[] = [];
  private listeners = new Map<
    string,
    Set<(event: { code?: number; data?: string; reason?: string; wasClean?: boolean }) => void>
  >();

  addEventListener(
    type: string,
    callback: (event: { code?: number; data?: string; reason?: string; wasClean?: boolean }) => void,
  ): void {
    const entries = this.listeners.get(type) ?? new Set();
    entries.add(callback);
    this.listeners.set(type, entries);
  }

  removeEventListener(
    type: string,
    callback: (event: { code?: number; data?: string; reason?: string; wasClean?: boolean }) => void,
  ): void {
    this.listeners.get(type)?.delete(callback);
  }

  close(code = 1000, reason = ""): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.emit("close", { code, reason, wasClean: code === 1000 });
  }

  send(payload: string): void {
    this.sent.push(payload);
  }

  open(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.emit("open", {});
  }

  message(payload: object): void {
    this.emit("message", { data: JSON.stringify(payload) });
  }

  private emit(
    type: string,
    event: { code?: number; data?: string; reason?: string; wasClean?: boolean },
  ): void {
    for (const callback of this.listeners.get(type) ?? []) callback(event);
  }
}

describe("JsonRpcGatewayClient", () => {
  it("aborts a connection attempt without waiting for the timeout", async () => {
    const socket = new FakeWebSocket();
    const controller = new AbortController();
    const client = new JsonRpcGatewayClient({
      socketFactory: () => socket as unknown as WebSocketLike,
    });

    const connecting = client.connect("ws://gateway", controller.signal);
    controller.abort();

    await expect(connecting).rejects.toMatchObject({ name: "AbortError" });
    expect(socket.readyState).toBe(FakeWebSocket.CLOSED);
    expect(client.connectionState).toBe("closed");

    socket.open();
    expect(client.connectionState).toBe("closed");
  });

  it("rejects when the socket closes while connecting and can reconnect", async () => {
    const closedSocket = new FakeWebSocket();
    const retrySocket = new FakeWebSocket();
    const sockets = [closedSocket, retrySocket];
    const client = new JsonRpcGatewayClient({
      socketFactory: () => sockets.shift() as unknown as WebSocketLike,
    });

    const closing = client.connect("ws://gateway");
    closedSocket.close();
    await expect(closing).rejects.toThrow("WebSocket closed");
    expect(client.connectionState).toBe("closed");

    const reconnecting = client.connect("ws://gateway");
    retrySocket.open();
    await expect(reconnecting).resolves.toBeUndefined();
    expect(client.connectionState).toBe("open");
  });

  it("reports only normalized lifecycle metadata when a socket closes", async () => {
    const socket = new FakeWebSocket();
    const lifecycle: object[] = [];
    const client = new JsonRpcGatewayClient({
      onLifecycle: (event) => lifecycle.push(event),
      socketFactory: () => socket as unknown as WebSocketLike,
    });

    const connecting = client.connect("ws://gateway?ticket=secret");
    socket.open();
    await connecting;
    const pending = client.request("session.history", { content: "private transcript" });
    socket.close(1006, "sensitive reason");

    await expect(pending).rejects.toThrow("WebSocket closed");
    expect(lifecycle).toEqual([
      {
        clientInitiated: false,
        code: 1006,
        event: "closed",
        opened: true,
        pendingCount: 1,
        wasClean: false,
      },
    ]);
    expect(JSON.stringify(lifecycle)).not.toContain("secret");
    expect(JSON.stringify(lifecycle)).not.toContain("private transcript");
    expect(JSON.stringify(lifecycle)).not.toContain("sensitive reason");
  });

  it("preserves JSON-RPC error codes for callers", async () => {
    const socket = new FakeWebSocket();
    const client = new JsonRpcGatewayClient({
      socketFactory: () => socket as unknown as WebSocketLike,
    });

    const connecting = client.connect("ws://gateway");
    socket.open();
    await connecting;

    const request = client.request("session.resume");
    const [{ id }] = socket.sent.map((payload) => JSON.parse(payload));
    socket.message({ error: { code: 4007, message: "session not found" }, id });

    await expect(request).rejects.toMatchObject({
      code: 4007,
      message: "session not found",
      name: JsonRpcGatewayError.name,
    });
  });
});
