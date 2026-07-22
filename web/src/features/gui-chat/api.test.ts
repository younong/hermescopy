import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  const gatewayInstances: FakeGatewayClient[] = [];
  const state = { attachMissing: false };

  class FakeJsonRpcGatewayError extends Error {
    readonly code?: number;

    constructor(message: string, code?: number) {
      super(message);
      this.code = code;
    }
  }

  class FakeGatewayClient {
    connectionState: "closed" | "open" = "closed";
    readonly connect = vi.fn(async () => {
      this.connectionState = "open";
    });
    readonly request = vi.fn(async (method: string, params: Record<string, unknown>) => {
      if (method === "session.attach") {
        if (state.attachMissing) throw new FakeJsonRpcGatewayError("Method not found", -32601);
        return {
          resume_kind: "live",
          resumed: params.session_id,
          session_id: `runtime-${String(params.session_id)}`,
          session_key: params.session_id,
          switch_generation: params.switch_generation,
        };
      }
      return { session_id: "runtime-new", stored_session_id: "stored-new" };
    });
    readonly close = vi.fn(() => {
      this.connectionState = "closed";
    });
    readonly onEvent = vi.fn(() => () => undefined);
    readonly onState = vi.fn(() => () => undefined);

    constructor() {
      gatewayInstances.push(this);
    }
  }

  return { FakeGatewayClient, FakeJsonRpcGatewayError, gatewayInstances, state };
});

vi.mock("@/lib/gatewayClient", () => ({
  GatewayClient: mocks.FakeGatewayClient,
  JsonRpcGatewayError: mocks.FakeJsonRpcGatewayError,
}));

vi.mock("@/lib/browserIdentity", () => ({
  getHermesBrowserId: () => "browser-test",
}));

import { connectGuiChat } from "./api";

beforeEach(() => {
  mocks.gatewayInstances.length = 0;
  mocks.state.attachMissing = false;
});

describe("connectGuiChat", () => {
  it("reuses one connection for repeated warm session attaches", async () => {
    const connection = connectGuiChat({ ownerKey: "owner-a", profile: "worker" });
    const firstStages: string[] = [];
    const secondStages: string[] = [];

    await connection.createOrAttach("stored-a", 1, undefined, {
      onSwitchStage: (stage) => firstStages.push(stage),
    });
    await connection.createOrAttach("stored-b", 2, undefined, {
      onSwitchStage: (stage) => secondStages.push(stage),
    });

    expect(mocks.gatewayInstances).toHaveLength(1);
    const client = mocks.gatewayInstances[0];
    expect(client.connect).toHaveBeenCalledOnce();
    expect(client.request).toHaveBeenNthCalledWith(
      1,
      "session.attach",
      expect.objectContaining({
        browser_id: "browser-test",
        profile: "worker",
        session_id: "stored-a",
        switch_generation: 1,
      }),
      undefined,
      undefined,
    );
    expect(client.request).toHaveBeenNthCalledWith(
      2,
      "session.attach",
      expect.objectContaining({
        session_id: "stored-b",
        switch_generation: 2,
      }),
      undefined,
      undefined,
    );
    expect(firstStages).not.toContain("connection.reused");
    expect(secondStages).toEqual([
      "connection.reused",
      "session.attach.start",
      "session.attach.end",
      "session.attach.live",
    ]);
  });

  it("falls back to a fresh socket only when session.attach is unavailable", async () => {
    mocks.state.attachMissing = true;
    const connection = connectGuiChat({ ownerKey: "owner-a" });

    await connection.createOrAttach("stored-a", 1);
    await connection.createOrAttach("stored-b", 2);

    const client = mocks.gatewayInstances[0];
    expect(client.connect).toHaveBeenCalledTimes(3);
    expect(client.close).toHaveBeenCalledTimes(2);
    expect(client.request.mock.calls.map((call) => call[0])).toEqual([
      "session.attach",
      "session.resume",
      "session.resume",
    ]);
  });

  it("does not fall back for attach errors other than method-not-found", async () => {
    const connection = connectGuiChat({ ownerKey: "owner-a" });
    const client = mocks.gatewayInstances[0];
    client.request.mockRejectedValueOnce(new mocks.FakeJsonRpcGatewayError("not found", 4007));

    await expect(connection.createOrAttach("stored-a", 1)).rejects.toMatchObject({ code: 4007 });
    expect(client.close).not.toHaveBeenCalled();
    expect(client.connect).toHaveBeenCalledOnce();
  });

  it("sends a sessionless heartbeat over the open connection", async () => {
    const connection = connectGuiChat({ ownerKey: "owner-a" });
    await connection.createOrAttach("stored-a", 1);

    await connection.ping();

    const client = mocks.gatewayInstances[0];
    expect(client.connect).toHaveBeenCalledOnce();
    expect(client.request).toHaveBeenLastCalledWith("gateway.ping", {}, 10_000);
  });

  it("reports frame diagnostics over the existing connection without awaiting failures", async () => {
    const connection = connectGuiChat({ ownerKey: "owner-a" });
    const client = mocks.gatewayInstances[0];
    client.request.mockRejectedValueOnce(new Error("offline"));

    expect(() => connection.reportFrameQueueDiagnostic({
      duration_ms: 10,
      graphemes_consumed: 1,
      graphemes_per_frame_max: 1,
      graphemes_per_frame_p95: 1,
      input_graphemes: 1,
      input_stream_events: 1,
      long_frames: 0,
      max_queued_events: 1,
      max_queued_graphemes: 1,
      outcome: "completed",
      render_frames: 1,
      schedule_delay_max_ms: 8,
      schedule_delay_p95_ms: 8,
      schema_version: 1,
    })).not.toThrow();

    expect(client.request).toHaveBeenCalledWith(
      "diagnostics.gui_frame_queue",
      expect.objectContaining({ schema_version: 1, outcome: "completed" }),
    );
    await Promise.resolve();
  });

  it("creates a new session on the same authenticated connection", async () => {
    const connection = connectGuiChat({ ownerKey: "owner-a" });
    await connection.createOrAttach(null, 1);
    await connection.createOrAttach(null, 2);

    const client = mocks.gatewayInstances[0];
    expect(client.connect).toHaveBeenCalledOnce();
    expect(client.request.mock.calls.map((call) => call[0])).toEqual([
      "session.create",
      "session.create",
    ]);
    expect(client.request).toHaveBeenNthCalledWith(
      1,
      "session.create",
      expect.objectContaining({
        browser_id: "browser-test",
        switch_generation: 1,
      }),
      undefined,
      undefined,
    );
    expect(client.request).toHaveBeenNthCalledWith(
      2,
      "session.create",
      expect.objectContaining({ switch_generation: 2 }),
      undefined,
      undefined,
    );
  });
});
