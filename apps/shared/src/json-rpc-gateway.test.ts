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
