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
  id: string;
  title?: string;
  mimeType?: string;
  url: string;
  messageId?: string;
  toolCallId?: string;
  width?: number;
  height?: number;
}

export interface ApprovalState {
  id: string;
  command?: string;
  description?: string;
  payload: unknown;
  status: "pending" | "approved" | "denied";
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
  artifacts: Record<string, ImageArtifactState>;
  approvals: Record<string, ApprovalState>;
  approvalOrder: string[];
  statusLines: string[];
  isGenerating: boolean;
  error?: string;
}

export const initialGuiChatState: GuiChatState = {
  approvals: {},
  approvalOrder: [],
  artifacts: {},
  connection: "idle",
  isGenerating: false,
  messages: [],
  statusLines: [],
  toolCalls: {},
  toolOrder: [],
};
