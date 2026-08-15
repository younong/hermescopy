import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => {
  const gatewayInstances: FakeGatewayClient[] = [];
  const state = { attachMissing: false };
  const compressImageForUpload = vi.fn(async (file: File) => file);
  const readFileAsDataUrl = vi.fn(async () => "data:image/jpeg;base64,Y29tcHJlc3NlZA==");

  class FakeJsonRpcGatewayError extends Error {
    readonly code?: number;

    constructor(message: string, code?: number) {
      super(message);
      this.code = code;
    }
  }

  class FakeGatewayClient {
    connectionState: "closed" | "open" = "closed";
    private readonly stateHandlers = new Set<(state: "closed" | "open") => void>();
    readonly connect = vi.fn(async () => {
      this.connectionState = "open";
    });
    readonly request = vi.fn(async (
      method: string,
      params: Record<string, unknown>,
    ): Promise<unknown> => {
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
    readonly onState = vi.fn((handler: (state: "closed" | "open") => void) => {
      this.stateHandlers.add(handler);
      return () => this.stateHandlers.delete(handler);
    });

    constructor() {
      gatewayInstances.push(this);
    }

    emitState(state: "closed" | "open"): void {
      this.connectionState = state;
      for (const handler of this.stateHandlers) handler(state);
    }
  }

  return {
    compressImageForUpload,
    FakeGatewayClient,
    FakeJsonRpcGatewayError,
    gatewayInstances,
    readFileAsDataUrl,
    state,
  };
});

vi.mock("../attachments", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../attachments")>()),
  compressImageForUpload: mocks.compressImageForUpload,
  readFileAsDataUrl: mocks.readFileAsDataUrl,
}));

vi.mock("@/lib/gatewayClient", () => ({
  GatewayClient: mocks.FakeGatewayClient,
  JsonRpcGatewayError: mocks.FakeJsonRpcGatewayError,
}));

vi.mock("@/lib/browserIdentity", () => ({
  getHermesBrowserId: () => "browser-test",
}));

import { connectGuiChat } from "../api";

beforeEach(() => {
  mocks.gatewayInstances.length = 0;
  mocks.state.attachMissing = false;
  mocks.compressImageForUpload.mockClear();
  mocks.readFileAsDataUrl.mockClear();
});

describe("connectGuiChat", () => {
  it("compresses an oversized image before sending it to the gateway", async () => {
    const connection = connectGuiChat({ ownerKey: "owner-a" });
    const original = new File(["original"], "photo.png", { type: "image/png" });
    const compressed = new File(["compressed"], "photo.jpg", { type: "image/jpeg" });
    mocks.compressImageForUpload.mockResolvedValueOnce(compressed);

    await connection.attachImage("runtime-a", original);

    expect(mocks.compressImageForUpload).toHaveBeenCalledWith(original);
    expect(mocks.readFileAsDataUrl).toHaveBeenCalledWith(compressed);
    expect(mocks.gatewayInstances[0].request).toHaveBeenCalledWith("image.attach_bytes", {
      content_base64: "Y29tcHJlc3NlZA==",
      filename: "photo.jpg",
      session_id: "runtime-a",
    });
  });

  it("attaches owner-scoped routes without creating a chat session", async () => {
    const connection = connectGuiChat({ ownerKey: "owner-a" });

    await connection.attachOwner();

    expect(mocks.gatewayInstances[0].request).toHaveBeenCalledWith(
      "session.owner_attach",
      { browser_id: "browser-test" },
    );
  });

  it("attaches the owner before concurrent collaboration requests", async () => {
    const connection = connectGuiChat({ ownerKey: "owner-a" });
    const client = mocks.gatewayInstances[0];
    const ownerAttach = deferred<void>();
    client.request.mockImplementation(async (method: string) => {
      if (method === "session.owner_attach") await ownerAttach.promise;
      return method === "collaboration.groups.list" ? { groups: [] } : {};
    });

    const groups = connection.collaboration.listGroups();
    const detail = connection.collaboration.getGroup("group-a");
    await vi.waitFor(() => {
      expect(client.request.mock.calls.map((call) => call[0])).toEqual(["session.owner_attach"]);
    });
    ownerAttach.resolve();
    await Promise.all([groups, detail]);

    const collaborationMethods = client.request.mock.calls.map((call) => call[0]);
    expect(collaborationMethods[0]).toBe("session.owner_attach");
    expect(new Set(collaborationMethods.slice(1))).toEqual(new Set([
      "collaboration.groups.list",
      "collaboration.group.get",
    ]));
  });

  it("waits for a direct session switch before collaboration", async () => {
    const connection = connectGuiChat({ ownerKey: "owner-a" });
    const client = mocks.gatewayInstances[0];
    const sessionAttach = deferred<Record<string, unknown>>();
    client.request.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === "session.attach") return sessionAttach.promise;
      if (method === "collaboration.groups.list") return { groups: [] };
      return { session_id: "runtime-new", stored_session_id: "stored-new", ...params };
    });

    const switching = connection.createOrAttach("stored-a", 1);
    const groups = connection.collaboration.listGroups();
    await vi.waitFor(() => {
      expect(client.request.mock.calls.map((call) => call[0])).toEqual(["session.attach"]);
    });
    sessionAttach.resolve({
      resume_kind: "live",
      resumed: "stored-a",
      session_id: "runtime-a",
      session_key: "stored-a",
      switch_generation: 1,
    });
    await Promise.all([switching, groups]);

    expect(client.request.mock.calls.map((call) => call[0])).toEqual([
      "session.attach",
      "session.owner_attach",
      "collaboration.groups.list",
    ]);
  });

  it("allows collaboration after a failed direct session switch", async () => {
    const connection = connectGuiChat({ ownerKey: "owner-a" });
    const client = mocks.gatewayInstances[0];
    client.request.mockRejectedValueOnce(new mocks.FakeJsonRpcGatewayError("not found", 4007));

    const switching = connection.createOrAttach("stored-a", 1);
    const groups = connection.collaboration.listGroups();

    await expect(switching).rejects.toMatchObject({ code: 4007 });
    await expect(groups).resolves.toEqual({ session_id: "runtime-new", stored_session_id: "stored-new" });
    expect(client.request.mock.calls.map((call) => call[0])).toEqual([
      "session.attach",
      "session.owner_attach",
      "collaboration.groups.list",
    ]);
  });

  it("reattaches the owner before collaboration after reconnect", async () => {
    const connection = connectGuiChat({ ownerKey: "owner-a" });
    const client = mocks.gatewayInstances[0];

    await connection.collaboration.listGroups();
    client.emitState("closed");
    await connection.collaboration.getGroup("group-a");

    expect(client.request.mock.calls.map((call) => call[0])).toEqual([
      "session.owner_attach",
      "collaboration.groups.list",
      "session.owner_attach",
      "collaboration.group.get",
    ]);
  });

  it("sends only the selected employee ID for a new direct chat", async () => {
    const connection = connectGuiChat({ ownerKey: "owner-a" });

    await connection.createOrAttach(
      null,
      1,
      undefined,
      undefined,
      { employeeId: "employee-a" },
    );

    expect(mocks.gatewayInstances[0].request).toHaveBeenCalledWith(
      "session.create",
      expect.objectContaining({
        employee_id: "employee-a",
        source: "dashboard-gui",
        switch_generation: 1,
      }),
      undefined,
      undefined,
    );
    expect(mocks.gatewayInstances[0].request.mock.calls[0]?.[1]).not.toHaveProperty(
      "employee_policy",
    );
  });

  it("reuses one connection for repeated warm session attaches", async () => {
    const connection = connectGuiChat({ ownerKey: "owner-a" });
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
    expect(client.request.mock.calls[0]?.[1]).not.toHaveProperty("display_history");
    expect(client.request.mock.calls.map((call) => call[0])).not.toContain("session.history");
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

  it("preflights and submits the selected registration at the message boundary", async () => {
    const connection = connectGuiChat({ ownerKey: "owner-a" });
    const registration = {
      id: "registration-a",
      provider: "provider-a",
      model: "model-a",
    };

    await connection.preflightModel("runtime-a", registration, {
      confirmExpensiveModel: true,
    });
    await connection.send("runtime-a", "hello", {
      confirmExpensiveModel: true,
      modelRegistration: registration,
    });

    expect(mocks.gatewayInstances[0].request).toHaveBeenNthCalledWith(
      1,
      "prompt.model_preflight",
      {
        confirm_expensive_model: true,
        model: "model-a",
        provider: "provider-a",
        registration_id: "registration-a",
        session_id: "runtime-a",
      },
    );
    expect(mocks.gatewayInstances[0].request).toHaveBeenNthCalledWith(
      2,
      "prompt.submit",
      {
        confirm_expensive_model: true,
        model: "model-a",
        provider: "provider-a",
        registration_id: "registration-a",
        session_id: "runtime-a",
        text: "hello",
      },
    );
  });

  it("sets the current session reasoning level through config.set", async () => {
    const connection = connectGuiChat({ ownerKey: "owner-a" });

    await connection.setReasoningLevel("runtime-a", "max");

    expect(mocks.gatewayInstances[0].request).toHaveBeenCalledWith("config.set", {
      key: "reasoning",
      session_id: "runtime-a",
      value: "max",
    });
  });

  it("can persist a model as the global default", async () => {
    const connection = connectGuiChat({ ownerKey: "owner-a" });

    await connection.setDefaultModel(
      "runtime-a",
      { id: "registration-a", provider: "provider-a", model: "model-a" },
    );

    expect(mocks.gatewayInstances[0].request).toHaveBeenCalledWith("config.set", {
      confirm_expensive_model: false,
      key: "model",
      registration_id: "registration-a",
      session_id: "runtime-a",
      value: "model-a --provider provider-a --global",
    });
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

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}
