import type { GatewayEvent, GatewayEventName } from "@/lib/gatewayClient";
import type {
  CollaborationApprovalResponse,
  CollaborationAttachmentResponse,
  CollaborationGroupsResponse,
  CollaborationSubmitResponse,
  CollaborationTargetResponse,
} from "./protocol";
import type {
  CollaborationApprovalChoice,
  CollaborationSnapshot,
  CollaborationSubmitMessage,
} from "./types";

interface CollaborationRequestClient {
  on<P = unknown>(type: GatewayEventName, handler: (event: GatewayEvent<P>) => void): () => void;
  request<T>(
    method: string,
    params?: Record<string, unknown>,
    timeoutMs?: number,
    signal?: AbortSignal,
  ): Promise<T>;
}

export interface CollaborationApi {
  listGroups(includeArchived?: boolean, signal?: AbortSignal): Promise<CollaborationGroupsResponse>;
  getGroup(groupId: string, afterSequence?: number, signal?: AbortSignal): Promise<CollaborationSnapshot>;
  createGroup(name: string, employeeIds: string[], clientIdempotencyKey: string): Promise<CollaborationSnapshot>;
  archiveGroup(groupId: string): Promise<CollaborationGroupsResponse["groups"][number]>;
  updateMembers(groupId: string, employeeIds: string[]): Promise<CollaborationSnapshot>;
  submitMessage(message: CollaborationSubmitMessage): Promise<CollaborationSubmitResponse>;
  uploadAttachment(groupId: string, file: File): Promise<CollaborationAttachmentResponse>;
  respondToApproval(approvalId: string, choice: CollaborationApprovalChoice): Promise<CollaborationApprovalResponse>;
  interruptTarget(targetId: string): Promise<CollaborationTargetResponse>;
  onEvent(handler: (event: GatewayEvent<Record<string, unknown>>) => void): () => void;
}

export function createCollaborationApi(
  client: CollaborationRequestClient,
  ensureConnected: (signal?: AbortSignal) => Promise<void>,
): CollaborationApi {
  const request = async <T>(
    method: string,
    params: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<T> => {
    await ensureConnected(signal);
    return client.request<T>(method, params, undefined, signal);
  };

  return {
    archiveGroup: async (groupId) => {
      const response = await request<{ group: CollaborationGroupsResponse["groups"][number] }>(
        "collaboration.group.archive",
        { group_id: groupId },
      );
      return response.group;
    },
    createGroup: (name, employeeIds, clientIdempotencyKey) =>
      request("collaboration.group.create", {
        client_idempotency_key: clientIdempotencyKey,
        employee_ids: employeeIds,
        name,
      }),
    getGroup: (groupId, afterSequence, signal) =>
      request(
        "collaboration.group.get",
        {
          group_id: groupId,
          ...(afterSequence === undefined ? {} : { after_sequence: afterSequence }),
        },
        signal,
      ),
    interruptTarget: (targetId) =>
      request("collaboration.target.interrupt", { target_id: targetId }),
    listGroups: (includeArchived = false, signal) =>
      request("collaboration.groups.list", { include_archived: includeArchived }, signal),
    onEvent: (handler) => {
      const removers = [
        "collaboration.group.changed",
        "collaboration.event.appended",
        "collaboration.target.changed",
        "collaboration.execution.delta",
        "collaboration.approval.changed",
      ].map((type) => client.on(type, handler));
      return () => removers.forEach((remove) => remove());
    },
    respondToApproval: (approvalId, choice) =>
      request("collaboration.approval.respond", { approval_id: approvalId, choice }),
    submitMessage: (message) =>
      request("collaboration.message.submit", { ...message }),
    updateMembers: (groupId, employeeIds) =>
      request("collaboration.members.update", { employee_ids: employeeIds, group_id: groupId }),
    uploadAttachment: async (groupId, file) => {
      const kind = attachmentKind(file);
      return request(`collaboration.${kind}.attach`, {
        content_base64: await fileBase64(file),
        filename: file.name,
        group_id: groupId,
        media_type: file.type || undefined,
      });
    },
  };
}

function attachmentKind(file: File): "image" | "pdf" | "file" {
  if (file.type.startsWith("image/")) return "image";
  if (file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf")) return "pdf";
  return "file";
}

async function fileBase64(file: File): Promise<string> {
  const buffer = await file.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunkSize = 32_768;
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
  }
  return btoa(binary);
}
