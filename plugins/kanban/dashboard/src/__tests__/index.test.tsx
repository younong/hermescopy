// @vitest-environment jsdom

import "./runtimeMock";
import { describe, expect, it, vi } from "vitest";
import { runtimeMocks } from "./runtimeMock";

describe("Kanban plugin entry", () => {
  it("registers one shared root for the dashboard and Chat workspace", async () => {
    vi.resetModules();
    await import("../index");

    expect(runtimeMocks.register).toHaveBeenCalledWith("kanban", expect.any(Function));
    expect(runtimeMocks.registerWorkspace).toHaveBeenCalledWith("kanban", "kanban", expect.any(Function));
    expect(runtimeMocks.register.mock.calls[0]?.[1]).toBe(runtimeMocks.registerWorkspace.mock.calls[0]?.[2]);
  });
});
