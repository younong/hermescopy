import { describe, expect, it } from "vitest";

import { normalizeMentionSelection, recipientLabel } from "./mentions";
import type { CollaborationMembership } from "./types";

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
  });

  it("preserves no-mention background messages", () => {
    const selection = normalizeMentionSelection({ mentionAll: false, membershipIds: [] }, memberships);
    expect(selection).toEqual({ mentionAll: false, membershipIds: [] });
    expect(recipientLabel(selection, {}, () => "ignored")).toBe("No recipients · background only");
  });
});
