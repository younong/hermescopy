import type { GatewayEvent } from "@/lib/gatewayClient";

export interface SessionInfoPayload {
  cwd?: string;
  model?: string;
  provider?: string;
  title?: string;
}

export interface GatewayTranscriptAttachment {
  kind?: unknown;
  mime_type?: unknown;
  name?: unknown;
  pages_attached?: unknown;
  path?: unknown;
  ref_text?: unknown;
  size_bytes?: unknown;
  source_paths?: unknown;
}

export interface GatewayTranscriptMessage {
  id?: string;
  role: "assistant" | "system" | "tool" | "user";
  text?: string;
  content?: unknown;
  attachments?: GatewayTranscriptAttachment[];
  name?: string;
  context?: string;
}

export interface HistoryPagePayload {
  cursor?: string | null;
  has_more: boolean;
  returned_count: number;
  truncated_count?: number;
  live_only?: boolean;
}

export type ClarifyOutcome = "answered" | "cancelled" | "timed_out";

export interface ClarifyRequestPayload {
  choices?: string[] | null;
  expires_at_ms?: number;
  question: string;
  request_id: string;
  timeout_ms?: number;
}

export interface ClarifyResolvedPayload {
  outcome: ClarifyOutcome;
  request_id: string;
}

export interface PendingClarifyPrompt extends ClarifyRequestPayload {
  type: "clarify";
}

export interface SessionCreateResponse {
  history_page?: HistoryPagePayload;
  info?: SessionInfoPayload;
  message_count?: number;
  messages?: GatewayTranscriptMessage[];
  pending_prompts?: PendingClarifyPrompt[];
  session_id: string;
  stored_session_id?: string;
  switch_generation?: number;
}

export interface SessionInflightTurn {
  assistant?: string;
  streaming?: boolean;
  user?: string;
}

export interface SessionResumeResponse extends SessionCreateResponse {
  inflight?: SessionInflightTurn | null;
  resumed?: string;
  running?: boolean;
  session_key?: string;
  status?: string;
}

export interface SessionAttachResponse extends SessionResumeResponse {
  resume_kind: "cold" | "live";
  switch_generation: number;
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
  path?: string;
  source?: { kind?: string; value?: string } | string;
  title?: string;
  tool_call_id?: string;
  toolCallId?: string;
  url?: string;
  width?: number;
}

export interface ArtifactFilePayload extends ArtifactImagePayload {
  filename?: string;
  name?: string;
}

export interface ErrorPayload {
  message?: string;
  phase?: string;
}

export type GuiGatewayEvent = GatewayEvent<
  | ApprovalPayload
  | ArtifactFilePayload
  | ClarifyRequestPayload
  | ClarifyResolvedPayload
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
  if (typeof message.text === "string") return message.text;
  return textFromTranscriptContent(message.content);
}

function textFromTranscriptContent(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content.map(textFromTranscriptContent).filter(Boolean).join("\n");
  }
  if (!content || typeof content !== "object") return "";
  const record = content as Record<string, unknown>;
  const text = record.text ?? record.content ?? record.input_text;
  if (typeof text === "string") return text;
  if (Array.isArray(text)) return textFromTranscriptContent(text);
  return "";
}
