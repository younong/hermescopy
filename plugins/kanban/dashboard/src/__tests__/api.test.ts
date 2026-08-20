import { beforeEach, describe, expect, it, vi } from "vitest";

import { runtimeMocks as mocks } from "./runtimeMock";

import { kanbanApi, parseKanbanEventEnvelope } from "../api";

function requestCalls() {
  return mocks.fetchJSON.mock.calls.map(([url, init]) => ({
    url,
    method: init?.method ?? "GET",
    body: init?.body,
  }));
}

beforeEach(() => {
  vi.clearAllMocks();
  mocks.fetchJSON.mockResolvedValue({});
  mocks.buildWsUrl.mockResolvedValue("wss://example.test/events");
});

describe("Kanban API", () => {
  it("includes the explicit board on every board-scoped JSON route", async () => {
    await kanbanApi.getBoard("default", { include_archived: true });
    await kanbanApi.getTask("project-a", "t/a", { type: "outcome", name: "completed" });
    await kanbanApi.createTask("project-a", { title: "Ship" });
    await kanbanApi.updateTask("project-a", "t/a", { status: "ready" });
    await kanbanApi.deleteTask("project-a", "t/a");
    await kanbanApi.bulkUpdate("project-a", { ids: ["t/a"], priority: 2 });
    await kanbanApi.addComment("project-a", "t/a", "note");
    await kanbanApi.addLink("project-a", "p/1", "c/1");
    await kanbanApi.deleteLink("project-a", "p/1", "c/1");
    await kanbanApi.getStats("project-a");
    await kanbanApi.getAssignees("project-a");
    await kanbanApi.dispatch("project-a", { dryRun: true, max: 3 });
    await kanbanApi.getDiagnostics("project-a", "error");
    await kanbanApi.listActiveWorkers("project-a");
    await kanbanApi.getRun("project-a", 7);
    await kanbanApi.inspectRun("project-a", 7);
    await kanbanApi.terminateRun("project-a", 7, "stuck");
    await kanbanApi.getTaskLog("project-a", "t/a", 4096);
    await kanbanApi.reclaimTask("project-a", "t/a", "retry");
    await kanbanApi.reassignTask("project-a", "t/a", { profile: "reviewer", reclaim_first: true });
    await kanbanApi.specifyTask("project-a", "t/a", "operator");
    await kanbanApi.decomposeTask("project-a", "t/a", "operator");
    await kanbanApi.getHomeChannels("project-a", "t/a");
    await kanbanApi.subscribeHome("project-a", "t/a", "slack/team");
    await kanbanApi.unsubscribeHome("project-a", "t/a", "slack/team");

    const calls = requestCalls();
    expect(calls).toHaveLength(25);
    for (const { url } of calls) {
      expect(new URL(url, "http://localhost").searchParams.get("board")).toMatch(/^(default|project-a)$/);
    }
    expect(calls[0]?.url).toBe(
      "/api/plugins/kanban/board?board=default&include_archived=true",
    );
    expect(calls[1]?.url).toBe(
      "/api/plugins/kanban/tasks/t%2Fa?board=project-a&run_state_type=outcome&run_state_name=completed",
    );
    expect(calls[8]?.url).toContain("parent_id=p%2F1&child_id=c%2F1");
    expect(calls[11]?.url).toContain("dry_run=true&max=3");
    expect(calls[23]?.url).toContain("home-subscribe/slack%2Fteam?board=project-a");
    expect(calls[24]?.url).toContain("home-subscribe/slack%2Fteam?board=project-a");
  });

  it("uses exact JSON bodies for task, recovery, and orchestration mutations", async () => {
    await kanbanApi.createTask("default", { title: "Task", parents: ["p1"], goal_mode: true });
    await kanbanApi.bulkUpdate("default", { ids: ["t1"], archive: true });
    await kanbanApi.terminateRun("default", 4);
    await kanbanApi.reclaimTask("default", "t1");
    await kanbanApi.specifyTask("default", "t1");
    await kanbanApi.decomposeTask("default", "t1", "me");
    await kanbanApi.updateOrchestration({ auto_decompose: false, orchestrator_profile: "lead" });

    expect(requestCalls().map(({ method, body }) => [method, body])).toEqual([
      ["POST", JSON.stringify({ title: "Task", parents: ["p1"], goal_mode: true })],
      ["POST", JSON.stringify({ ids: ["t1"], archive: true })],
      ["POST", JSON.stringify({ reason: null })],
      ["POST", JSON.stringify({ reason: null })],
      ["POST", JSON.stringify({ author: null })],
      ["POST", JSON.stringify({ author: "me" })],
      ["PUT", JSON.stringify({ auto_decompose: false, orchestrator_profile: "lead" })],
    ]);
  });

  it("uses auth-aware multipart, download, and websocket helpers", async () => {
    const file = new File(["hello"], "brief.txt", { type: "text/plain" });
    mocks.authedFetch.mockResolvedValue(new Response("file", { status: 200 }));

    await kanbanApi.uploadAttachment("default", "t1", file, "operator");
    await kanbanApi.downloadAttachment("default", 9);
    await kanbanApi.buildEventsUrl("default", 42);

    const upload = mocks.fetchJSON.mock.calls[0];
    expect(upload?.[0]).toBe("/api/plugins/kanban/tasks/t1/attachments?board=default");
    expect(upload?.[1]?.body).toBeInstanceOf(FormData);
    const uploadedFile = (upload?.[1]?.body as FormData).get("file") as File;
    expect(uploadedFile.name).toBe(file.name);
    expect(uploadedFile.type).toBe(file.type);
    expect(uploadedFile.size).toBe(file.size);
    expect((upload?.[1]?.body as FormData).get("uploaded_by")).toBe("operator");
    expect(mocks.authedFetch).toHaveBeenCalledWith(
      "/api/plugins/kanban/attachments/9?board=default",
      { signal: undefined },
    );
    expect(mocks.buildWsUrl).toHaveBeenCalledWith(
      "/api/plugins/kanban/events",
      { board: "default", since: "42" },
      { signal: undefined },
    );
  });

  it("parses only valid websocket event envelopes", () => {
    expect(parseKanbanEventEnvelope(JSON.stringify({ events: [], cursor: 4 }))).toEqual({
      events: [],
      cursor: 4,
    });
    expect(parseKanbanEventEnvelope("not json")).toBeNull();
    expect(parseKanbanEventEnvelope(JSON.stringify({ events: {}, cursor: 4 }))).toBeNull();
    expect(parseKanbanEventEnvelope(new Blob())).toBeNull();
  });
});
