import type { GatewayEvent } from "@/lib/gatewayClient";
import type {
  CollaborationApproval,
  CollaborationAttachment,
  CollaborationEvent,
  CollaborationGroup,
  CollaborationSnapshot,
  CollaborationTarget,
} from "./types";

export type CollaborationGatewayEventType =
  | "collaboration.group.changed"
  | "collaboration.event.appended"
  | "collaboration.target.changed"
  | "collaboration.execution.delta"
  | "collaboration.approval.changed";

export interface CollaborationExecutionDelta {
  group_id: string;
  turn_id: string;
  target_id: string;
  execution_id: string;
  text: string;
}

export interface CollaborationSubmitResponse {
  event: CollaborationEvent;
  turn: CollaborationSnapshot["turns"][number] & { targets?: CollaborationTarget[] } | null;
}

export interface CollaborationGroupsResponse {
  groups: CollaborationGroup[];
}

export interface CollaborationAttachmentResponse {
  attachment: CollaborationAttachment;
}

export interface CollaborationApprovalResponse {
  approval: CollaborationApproval;
}

export interface CollaborationTargetResponse {
  target: CollaborationTarget;
}

export function isCollaborationEvent(
  event: GatewayEvent,
): event is GatewayEvent<Record<string, unknown>> & { type: CollaborationGatewayEventType } {
  return event.type.startsWith("collaboration.");
}

export function collaborationGroupId(event: GatewayEvent<Record<string, unknown>>): string | null {
  const value = event.payload?.group_id;
  return typeof value === "string" && value ? value : null;
}
