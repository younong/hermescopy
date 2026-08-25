import { describe, expect, it, vi } from "vitest";

import type { GatewayEvent } from "@/lib/gatewayClient";
import { createCollaborationApi } from "../api";

function harness() {
  const request = vi.fn().mockResolvedValue({});
  const on = vi.fn((...args: [string, (event: GatewayEvent) => void]) => {
    void args;
    return vi.fn();
  });
  const ensureConnected = vi.fn().mockResolvedValue(undefined);
  return {
    api: createCollaborationApi({ on, request }, ensureConnected),
    ensureConnected,
    on,
    request,
  };
}

describe("collaboration API", () => {
  it("uses exact group and message RPC request shapes", async () => {
    const { api, ensureConnected, request } = harness();

    await api.listGroups(true);
    await api.getGroup("group-a", { after_sequence: 12 });
    await api.createGroup("Research", ["account-b", "account-a"], "create-key");
    await api.updateMembers("group-a", ["account-a"]);
    await api.submitMessage({
      attachment_ids: ["attachment-a"],
      client_idempotency_key: "client-key",
      group_id: "group-a",
      mention_all: false,
      mentioned_membership_ids: ["membership-a"],
      text: "Coordinate",
    });

    expect(request.mock.calls.map(([method, params]) => [method, params])).toEqual([
      ["collaboration.groups.list", { include_archived: true }],
      ["collaboration.group.get", { after_sequence: 12, group_id: "group-a" }],
      ["collaboration.group.create", {
        employee_ids: ["account-b", "account-a"],
        client_idempotency_key: "create-key",
        name: "Research",
      }],
      ["collaboration.members.update", { employee_ids: ["account-a"], group_id: "group-a" }],
      ["collaboration.message.submit", {
        attachment_ids: ["attachment-a"],
        client_idempotency_key: "client-key",
        group_id: "group-a",
        mention_all: false,
        mentioned_membership_ids: ["membership-a"],
        text: "Coordinate",
      }],
    ]);
    expect(ensureConnected).toHaveBeenCalledTimes(5);
  });

  it("uses public durable IDs for archive, target, and approval mutations", async () => {
    const { api, request } = harness();

    await api.archiveGroup("group-a");
    await api.interruptTarget("target-a");
    await api.respondToApproval("approval-a", "session");

    expect(request.mock.calls.map(([method, params]) => [method, params])).toEqual([
      ["collaboration.group.archive", { group_id: "group-a" }],
      ["collaboration.target.interrupt", { target_id: "target-a" }],
      ["collaboration.approval.respond", { approval_id: "approval-a", choice: "session" }],
    ]);
  });

  it("uses typed attachment RPCs with exact browser-safe byte payloads", async () => {
    const { api, request } = harness();
    const image = new File([new Uint8Array([1, 2, 3])], "diagram.png", { type: "image/png" });
    const pdf = new File([new Uint8Array([4])], "brief.pdf", { type: "application/pdf" });
    const file = new File([new Uint8Array([5])], "notes.txt", { type: "text/plain" });

    await api.uploadAttachment("group-a", image);
    await api.uploadAttachment("group-a", pdf);
    await api.uploadAttachment("group-a", file);

    expect(request.mock.calls.map(([method, params]) => [method, params])).toEqual([
      ["collaboration.image.attach", {
        content_base64: "AQID",
        filename: "diagram.png",
        group_id: "group-a",
        media_type: "image/png",
      }],
      ["collaboration.pdf.attach", {
        content_base64: "BA==",
        filename: "brief.pdf",
        group_id: "group-a",
        media_type: "application/pdf",
      }],
      ["collaboration.file.attach", {
        content_base64: "BQ==",
        filename: "notes.txt",
        group_id: "group-a",
        media_type: "text/plain",
      }],
    ]);
  });

  it("routes supported collaboration event names through the supplied shared client", () => {
    const { api, on } = harness();
    const remove = api.onEvent(vi.fn());

    expect(on.mock.calls.map(([type]) => type)).toEqual([
      "collaboration.group.changed",
      "collaboration.event.appended",
      "collaboration.target.changed",
      "collaboration.execution.delta",
      "collaboration.approval.changed",
    ]);
    remove();
    for (const call of on.mock.results) expect(call.value).toHaveBeenCalledOnce();
  });
});
