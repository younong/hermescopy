import { describe, expect, it } from "vitest";

import { JsonRpcGatewayClient, JsonRpcGatewayError, type WebSocketLike } from "./json-rpc-gateway";

class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 3;

  readyState = FakeWebSocket.CONNECTING;
  sent: string[] = [];
  private listeners = new Map<string, Set<(event: { data?: string }) => void>>();

  addEventListener(type: string, callback: (event: { data?: string }) => void): void {
    const entries = this.listeners.get(type) ?? new Set();
    entries.add(callback);
    this.listeners.set(type, entries);
  }

  removeEventListener(type: string, callback: (event: { data?: string }) => void): void {
    this.listeners.get(type)?.delete(callback);
  }

  close(): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.emit("close", {});
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

  private emit(type: string, event: { data?: string }): void {
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
