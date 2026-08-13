import type { CollaborationEvent, CollaborationMembership } from "./types";

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

export function defaultMentionSelection(
  selection: MentionSelection,
  activeMemberships: CollaborationMembership[],
  events: CollaborationEvent[],
): MentionSelection {
  const normalized = normalizeMentionSelection(selection, activeMemberships);
  if (normalized.mentionAll || normalized.membershipIds.length > 0) return normalized;

  const activeIds = new Set(activeMemberships.map((member) => member.membership_id));
  let membershipId: string | undefined;
  let latestSequence = 0;
  for (const event of events) {
    let candidate: string | undefined;
    if (
      event.event_kind === "message.employee"
      && event.actor_membership_id
      && activeIds.has(event.actor_membership_id)
    ) {
      candidate = event.actor_membership_id;
    } else if (event.event_kind === "message.owner" && !event.body.mention_all) {
      const mentions = event.body.mentions;
      if (mentions?.length === 1 && activeIds.has(mentions[0])) candidate = mentions[0];
    }
    if (candidate && event.sequence > latestSequence) {
      membershipId = candidate;
      latestSequence = event.sequence;
    }
  }
  membershipId ??= activeMemberships[0]?.membership_id;
  return { mentionAll: false, membershipIds: membershipId ? [membershipId] : [] };
}

interface MentionLabels {
  mentionAll: string;
  noAvailableEmployee: string;
}

export function mentionLabel(
  selection: MentionSelection,
  membershipsById: Record<string, CollaborationMembership>,
  employeeName: (employeeId: string) => string,
  labels: Pick<MentionLabels, "mentionAll">,
): string {
  if (selection.mentionAll) return labels.mentionAll;
  return selection.membershipIds
    .map((id) => membershipsById[id])
    .filter(Boolean)
    .map((member) => `@${employeeName(member.employee_id)}`)
    .join(", ");
}

export function recipientLabel(
  selection: MentionSelection,
  membershipsById: Record<string, CollaborationMembership>,
  employeeName: (employeeId: string) => string,
  labels: MentionLabels,
): string {
  return mentionLabel(selection, membershipsById, employeeName, labels)
    || labels.noAvailableEmployee;
}
