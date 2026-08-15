import { describe, expect, it } from "vitest";

import { defaultMentionSelection, mentionLabel, normalizeMentionSelection, recipientLabel } from "../mentions";
import type { CollaborationEvent, CollaborationMembership } from "../types";

function employeeReply(sequence: number, employeeId: string): CollaborationEvent {
  return {
    actor_employee_id: employeeId,
    actor_kind: "employee",
    actor_membership_id: employeeId === "account-a" ? "membership-a" : "membership-b",
    body: { text: "Reply" },
    created_at: sequence,
    event_id: `event-${sequence}`,
    event_kind: "message.employee",
    group_id: "group-a",
    sequence,
  };
}

const memberships: CollaborationMembership[] = [
  {
    employee_id: "account-a",
    created_at: 1,
    group_id: "group-a",
    join_sequence: 1,
    leave_sequence: null,
    left_at: null,
    membership_id: "membership-a",
    profile_fingerprint: "fingerprint-a",
    profile_revision: 1,
    role: "Researcher",
  },
  {
    employee_id: "account-b",
    created_at: 1,
    group_id: "group-a",
    join_sequence: 1,
    leave_sequence: null,
    left_at: null,
    membership_id: "membership-b",
    profile_fingerprint: "fingerprint-b",
    profile_revision: 1,
    role: "Writer",
  },
];

describe("structured collaboration mentions", () => {
  it("keeps explicit durable membership IDs and removes duplicates or stale IDs", () => {
    expect(normalizeMentionSelection({
      mentionAll: false,
      membershipIds: ["membership-b", "membership-a", "membership-b", "stale"],
    }, memberships)).toEqual({
      mentionAll: false,
      membershipIds: ["membership-b", "membership-a"],
    });
  });

  it("represents @all independently of free text", () => {
    expect(normalizeMentionSelection({
      mentionAll: true,
      membershipIds: ["membership-a"],
    }, memberships)).toEqual({ mentionAll: true, membershipIds: [] });
    expect(mentionLabel(
      { mentionAll: true, membershipIds: [] },
      {},
      () => "ignored",
      { mentionAll: "@所有人" },
    )).toBe("@所有人");
  });

  it("formats selected employees without adding them to message text", () => {
    expect(mentionLabel(
      { mentionAll: false, membershipIds: ["membership-a", "membership-b"] },
      Object.fromEntries(memberships.map((member) => [member.membership_id, member])),
      (id) => id,
      { mentionAll: "@所有人" },
    )).toBe("@account-a, @account-b");
  });

  it("routes the first unmentioned message to the first available employee", () => {
    expect(defaultMentionSelection(
      { mentionAll: false, membershipIds: [] },
      memberships,
      [],
    )).toEqual({ mentionAll: false, membershipIds: ["membership-a"] });
  });

  it("keeps an explicitly selected employee current", () => {
    const explicitMessage: CollaborationEvent = {
      actor_employee_id: null,
      actor_kind: "owner",
      actor_membership_id: null,
      body: { mention_all: false, mentions: ["membership-b"], text: "Start" },
      created_at: 1,
      event_id: "event-owner",
      event_kind: "message.owner",
      group_id: "group-a",
      sequence: 1,
    };
    expect(defaultMentionSelection(
      { mentionAll: false, membershipIds: [] },
      memberships,
      [explicitMessage],
    )).toEqual({ mentionAll: false, membershipIds: ["membership-b"] });
  });

  it("routes later unmentioned messages to the latest employee who replied", () => {
    const events = [
      employeeReply(2, "account-b"),
      employeeReply(3, "account-a"),
    ];
    expect(defaultMentionSelection(
      { mentionAll: false, membershipIds: [] },
      memberships,
      events,
    )).toEqual({ mentionAll: false, membershipIds: ["membership-a"] });
  });

  it("preserves explicit mentions and @all", () => {
    expect(defaultMentionSelection(
      { mentionAll: false, membershipIds: ["membership-b"] },
      memberships,
      [],
    )).toEqual({ mentionAll: false, membershipIds: ["membership-b"] });
    expect(defaultMentionSelection(
      { mentionAll: true, membershipIds: ["membership-a"] },
      memberships,
      [],
    )).toEqual({ mentionAll: true, membershipIds: [] });
  });

  it("reports when no employee is available using localized labels", () => {
    const selection = defaultMentionSelection(
      { mentionAll: false, membershipIds: [] },
      [],
      [],
    );
    expect(selection).toEqual({ mentionAll: false, membershipIds: [] });
    expect(recipientLabel(selection, {}, () => "ignored", {
      mentionAll: "@所有人",
      noAvailableEmployee: "暂无可用员工",
    })).toBe("暂无可用员工");
  });
});
