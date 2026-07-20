import type { ConnectionState } from "@/lib/gatewayClient";

export type GuiChatConnectionState = ConnectionState;

export type ChatMessageRole = "user" | "assistant" | "system";

export interface ChatMessage {
  id: string;
  role: ChatMessageRole;
  text: string;
  streaming?: boolean;
  status?: "complete" | "error" | "interrupted";
  artifactIds: string[];
  attachments?: MessageAttachmentState[];
}

export interface MessageAttachmentState {
  id: string;
  kind: GuiComposerAttachmentKind;
  name: string;
  mimeType?: string;
  sizeBytes: number;
  previewUrl?: string;
  pagesAttached?: number;
  refText?: string;
  sourcePath?: string;
  downloadUrl?: string;
}

export type ToolCallStatus = "running" | "succeeded" | "failed";

export interface ToolCallState {
  id: string;
  name: string;
  status: ToolCallStatus;
  input?: unknown;
  argsText?: string;
  output: string;
  summary?: string;
  error?: string;
  durationSeconds?: number;
  result?: unknown;
  artifactIds: string[];
}

export interface ImageArtifactState {
  kind?: "image";
  id: string;
  title?: string;
  mimeType?: string;
  url: string;
  downloadUrl?: string;
  messageId?: string;
  toolCallId?: string;
  width?: number;
  height?: number;
}

export interface FileArtifactState {
  kind: "file";
  id: string;
  name: string;
  mimeType?: string;
  sourcePath: string;
  downloadUrl: string;
  messageId?: string;
  toolCallId?: string;
}

export type ArtifactState = ImageArtifactState | FileArtifactState;

export interface ApprovalState {
  id: string;
  command?: string;
  description?: string;
  payload: unknown;
  status: "pending" | "approved" | "denied";
}

export type GuiComposerAttachmentKind = "image" | "pdf" | "file";

export type GuiComposerAttachmentStatus = "queued" | "uploading" | "uploaded" | "error";

export interface GuiComposerAttachment {
  id: string;
  file: File;
  kind: GuiComposerAttachmentKind;
  name: string;
  mimeType: string;
  sizeBytes: number;
  previewUrl?: string;
  status: GuiComposerAttachmentStatus;
  error?: string;
  stagedSessionId?: string;
  attachedPath?: string;
  pagesAttached?: number;
  refText?: string;
}

export interface GuiChatState {
  connection: GuiChatConnectionState;
  sessionId?: string;
  storedSessionId?: string;
  cwd?: string;
  model?: string;
  messages: ChatMessage[];
  toolCalls: Record<string, ToolCallState>;
  toolOrder: string[];
  artifacts: Record<string, ArtifactState>;
  approvals: Record<string, ApprovalState>;
  approvalOrder: string[];
  statusLines: string[];
  isGenerating: boolean;
  error?: string;
  historyCursor?: string;
  historyHasMore: boolean;
  historyLoading: boolean;
  historyError?: string;
  safeguardReached: boolean;
  loadedTextChars: number;
}

export const initialGuiChatState: GuiChatState = {
  approvals: {},
  approvalOrder: [],
  artifacts: {},
  connection: "idle",
  historyHasMore: false,
  historyLoading: false,
  isGenerating: false,
  loadedTextChars: 0,
  messages: [],
  safeguardReached: false,
  statusLines: [],
  toolCalls: {},
  toolOrder: [],
};
