import type { CollaborationMembership } from "./types";

export interface MentionSelection {
  mentionAll: boolean;
  membershipIds: string[];
}

export function normalizeMentionSelection(
  selection: MentionSelection,
  activeMemberships: CollaborationMembership[],
): MentionSelection {
  if (selection.mentionAll) return { mentionAll: true, membershipIds: [] };
  const active = new Set(activeMemberships.map((member) => member.membership_id));
  return {
    mentionAll: false,
    membershipIds: [...new Set(selection.membershipIds)].filter((id) => active.has(id)),
  };
}

export function recipientLabel(
  selection: MentionSelection,
  membershipsById: Record<string, CollaborationMembership>,
  employeeName: (employeeId: string) => string,
): string {
  if (selection.mentionAll) return "@all";
  if (selection.membershipIds.length === 0) return "No recipients · background only";
  return selection.membershipIds
    .map((id) => membershipsById[id])
    .filter(Boolean)
    .map((member) => `@${employeeName(member.employee_id)}`)
    .join(", ");
}
