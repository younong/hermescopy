import type { GatewayEvent } from "@/lib/gatewayClient";

export interface SessionInfoPayload {
  cwd?: string;
  model?: string;
  provider?: string;
  title?: string;
}

export interface GatewayTranscriptMessage {
  role: "assistant" | "system" | "tool" | "user";
  text?: string;
  content?: string;
  name?: string;
  context?: string;
}

export interface SessionCreateResponse {
  info?: SessionInfoPayload;
  message_count?: number;
  messages?: GatewayTranscriptMessage[];
  session_id: string;
  stored_session_id?: string;
}

export interface SessionResumeResponse extends SessionCreateResponse {
  resumed?: string;
  running?: boolean;
  session_key?: string;
  status?: string;
}

export interface MessageDeltaPayload {
  rendered?: string;
  text?: string;
}

export interface MessageCompletePayload {
  reasoning?: string;
  rendered?: string;
  status?: "complete" | "error" | "interrupted" | string;
  text?: string;
  usage?: unknown;
  warning?: string;
}

export interface ToolStartPayload {
  args_text?: string;
  context?: unknown;
  id?: string;
  input?: unknown;
  name?: string;
  tool_id?: string;
  title?: string;
}

export interface ToolCompletePayload {
  duration_s?: number;
  error?: string | null;
  id?: string;
  name?: string;
  ok?: boolean;
  output?: string;
  result?: unknown;
  result_text?: string;
  status?: string;
  summary?: string;
  tool_id?: string;
}

export interface StatusPayload {
  kind?: string;
  text?: string;
}

export interface ApprovalPayload {
  allow_permanent?: boolean;
  command?: string;
  description?: string;
  id?: string;
  request_id?: string;
}

export interface ArtifactImagePayload {
  height?: number;
  id?: string;
  message_id?: string;
  messageId?: string;
  mime_type?: string;
  mimeType?: string;
  source?: { kind?: string; value?: string } | string;
  title?: string;
  tool_call_id?: string;
  toolCallId?: string;
  url?: string;
  width?: number;
}

export interface ErrorPayload {
  message?: string;
  phase?: string;
}

export type GuiGatewayEvent = GatewayEvent<
  | ApprovalPayload
  | ArtifactImagePayload
  | ErrorPayload
  | MessageCompletePayload
  | MessageDeltaPayload
  | SessionInfoPayload
  | StatusPayload
  | ToolCompletePayload
  | ToolStartPayload
  | unknown
>;

export function textFromTranscriptMessage(message: GatewayTranscriptMessage): string {
  return message.text ?? message.content ?? "";
}
