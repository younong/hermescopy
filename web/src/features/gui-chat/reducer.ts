import type { GatewayEvent } from "@/lib/gatewayClient";
import type {
  ApprovalPayload,
  ArtifactImagePayload,
  ErrorPayload,
  GatewayTranscriptMessage,
  MessageCompletePayload,
  MessageDeltaPayload,
  SessionCreateResponse,
  SessionInfoPayload,
  SessionResumeResponse,
  StatusPayload,
  ToolCompletePayload,
  ToolStartPayload,
} from "./protocol";
import { textFromTranscriptMessage } from "./protocol";
import {
  initialGuiChatState,
  type ApprovalState,
  type ChatMessage,
  type GuiChatConnectionState,
  type GuiChatState,
  type ImageArtifactState,
  type ToolCallState,
} from "./types";

export type GuiChatAction =
  | { type: "connection"; state: GuiChatConnectionState }
  | { type: "session.created"; response: SessionCreateResponse | SessionResumeResponse }
  | { type: "event"; event: GatewayEvent }
  | { type: "user.sent"; id: string; text: string }
  | { type: "error"; message: string }
  | { type: "approval.resolved"; id: string; approved: boolean }
  | { type: "reset" };

export function guiChatReducer(
  state: GuiChatState,
  action: GuiChatAction,
): GuiChatState {
  switch (action.type) {
    case "connection":
      return { ...state, connection: action.state };
    case "session.created":
      return applySessionResponse(state, action.response);
    case "event":
      return applyGatewayEvent(state, action.event);
    case "user.sent":
      return appendMessage(state, {
        artifactIds: [],
        id: action.id,
        role: "user",
        text: action.text,
      });
    case "error":
      return { ...state, error: action.message };
    case "approval.resolved":
      return updateApproval(state, action.id, {
        status: action.approved ? "approved" : "denied",
      });
    case "reset":
      return initialGuiChatState;
  }
}

function applySessionResponse(
  state: GuiChatState,
  response: SessionCreateResponse | SessionResumeResponse,
): GuiChatState {
  const messages = Array.isArray(response.messages)
    ? response.messages.flatMap(transcriptToMessage)
    : state.messages;

  return {
    ...state,
    error: undefined,
    isGenerating: !!("running" in response && response.running),
    messages,
    model: response.info?.model ?? state.model,
    sessionId: response.session_id,
    storedSessionId:
      response.stored_session_id ??
      ("session_key" in response ? response.session_key : undefined) ??
      ("resumed" in response ? response.resumed : undefined) ??
      state.storedSessionId,
  };
}

function applyGatewayEvent(state: GuiChatState, event: GatewayEvent): GuiChatState {
  if (event.session_id && state.sessionId && event.session_id !== state.sessionId) {
    return state;
  }

  switch (event.type) {
    case "session.info":
      return applySessionInfo(state, event.payload as SessionInfoPayload | undefined);
    case "message.start":
      return startAssistantMessage(state);
    case "message.delta":
      return appendAssistantDelta(state, event.payload as MessageDeltaPayload | undefined);
    case "message.complete":
      return completeAssistantMessage(
        state,
        event.payload as MessageCompletePayload | undefined,
      );
    case "thinking.delta":
    case "reasoning.delta":
    case "status.update":
      return appendStatusLine(state, event.payload as StatusPayload | undefined);
    case "tool.start":
      return startToolCall(state, event.payload as ToolStartPayload | undefined);
    case "tool.progress":
      return appendToolProgress(state, event.payload as StatusPayload | undefined);
    case "tool.complete":
      return completeToolCall(state, event.payload as ToolCompletePayload | undefined);
    case "approval.request":
      return addApproval(state, event.payload as ApprovalPayload | undefined);
    case "approval.resolved":
      return resolveApprovalFromEvent(state, event.payload);
    case "artifact.image":
    case "artifact.created":
      return addImageArtifact(state, event.payload as ArtifactImagePayload | undefined);
    case "error":
      return applyError(state, event.payload as ErrorPayload | undefined);
    default:
      return state;
  }
}

function transcriptToMessage(message: GatewayTranscriptMessage, index: number): ChatMessage[] {
  if (message.role === "tool") {
    return [];
  }
  const text = textFromTranscriptMessage(message);
  if (!text.trim()) {
    return [];
  }
  return [
    {
      artifactIds: [],
      id: `history-${index}`,
      role: message.role === "user" ? "user" : message.role === "assistant" ? "assistant" : "system",
      text,
    },
  ];
}

function appendMessage(state: GuiChatState, message: ChatMessage): GuiChatState {
  return { ...state, messages: [...state.messages, message] };
}

function applySessionInfo(
  state: GuiChatState,
  payload: SessionInfoPayload | undefined,
): GuiChatState {
  if (!payload) return state;
  return { ...state, model: payload.model ?? state.model };
}

function startAssistantMessage(state: GuiChatState): GuiChatState {
  if (state.messages.at(-1)?.streaming) {
    return { ...state, isGenerating: true };
  }
  return appendMessage(
    { ...state, isGenerating: true, error: undefined },
    {
      artifactIds: [],
      id: createClientId("assistant"),
      role: "assistant",
      streaming: true,
      text: "",
    },
  );
}

function appendAssistantDelta(
  state: GuiChatState,
  payload: MessageDeltaPayload | undefined,
): GuiChatState {
  const delta = payload?.text ?? payload?.rendered ?? "";
  if (!delta) return state;
  const working = state.messages.at(-1)?.role === "assistant" ? state : startAssistantMessage(state);
  const idx = working.messages.length - 1;
  const messages = working.messages.map((message, i) =>
    i === idx ? { ...message, streaming: true, text: message.text + delta } : message,
  );
  return { ...working, isGenerating: true, messages };
}

function completeAssistantMessage(
  state: GuiChatState,
  payload: MessageCompletePayload | undefined,
): GuiChatState {
  const status = normalizeMessageStatus(payload?.status);
  const finalText = payload?.text ?? payload?.rendered;
  let working = state;
  if (working.messages.at(-1)?.role !== "assistant") {
    working = startAssistantMessage(working);
  }
  const idx = working.messages.length - 1;
  const messages = working.messages.map((message, i) =>
    i === idx
      ? {
          ...message,
          status,
          streaming: false,
          text: finalText !== undefined && finalText !== message.text ? finalText : message.text,
        }
      : message,
  );
  const statusLines = payload?.warning
    ? [...working.statusLines, payload.warning].slice(-8)
    : working.statusLines;
  return { ...working, isGenerating: false, messages, statusLines };
}

function normalizeMessageStatus(status: string | undefined): ChatMessage["status"] {
  if (status === "error" || status === "interrupted") return status;
  return "complete";
}

function appendStatusLine(
  state: GuiChatState,
  payload: StatusPayload | undefined,
): GuiChatState {
  const text = payload?.text ?? (typeof payload === "string" ? payload : "");
  if (!text.trim()) return state;
  return { ...state, statusLines: [...state.statusLines, text].slice(-8) };
}

function toolId(payload: ToolStartPayload | ToolCompletePayload | undefined): string {
  return String(payload?.tool_id ?? payload?.id ?? createClientId("tool"));
}

function startToolCall(
  state: GuiChatState,
  payload: ToolStartPayload | undefined,
): GuiChatState {
  const id = toolId(payload);
  const existing = state.toolCalls[id];
  const next: ToolCallState = {
    artifactIds: existing?.artifactIds ?? [],
    argsText: payload?.args_text ?? existing?.argsText,
    id,
    input: payload?.input ?? payload?.context ?? existing?.input,
    name: payload?.title ?? payload?.name ?? existing?.name ?? "Tool",
    output: existing?.output ?? "",
    status: "running",
  };
  return {
    ...state,
    toolCalls: { ...state.toolCalls, [id]: next },
    toolOrder: state.toolOrder.includes(id) ? state.toolOrder : [...state.toolOrder, id],
  };
}

function appendToolProgress(
  state: GuiChatState,
  payload: StatusPayload | undefined,
): GuiChatState {
  const id = state.toolOrder.at(-1);
  const text = payload?.text ?? "";
  if (!id || !text) return appendStatusLine(state, payload);
  const tool = state.toolCalls[id];
  if (!tool || tool.status !== "running") return appendStatusLine(state, payload);
  return {
    ...state,
    toolCalls: {
      ...state.toolCalls,
      [id]: { ...tool, output: `${tool.output}${tool.output ? "\n" : ""}${text}` },
    },
  };
}

function completeToolCall(
  state: GuiChatState,
  payload: ToolCompletePayload | undefined,
): GuiChatState {
  const id = toolId(payload);
  const existing = state.toolCalls[id] ?? {
    artifactIds: [],
    id,
    name: payload?.name ?? "Tool",
    output: "",
    status: "running" as const,
  };
  const failed = Boolean(payload?.error) || payload?.ok === false || payload?.status === "error";
  const output = payload?.result_text ?? payload?.output ?? existing.output;
  const result = payload?.result ?? existing.result;
  const nextTool: ToolCallState = {
    ...existing,
    durationSeconds: payload?.duration_s ?? existing.durationSeconds,
    error: payload?.error ?? existing.error,
    name: payload?.name ?? existing.name,
    output,
    result,
    status: failed ? "failed" : "succeeded",
    summary: payload?.summary ?? existing.summary,
  };
  const nextToolCalls = {
    ...state.toolCalls,
    [id]: nextTool,
  };
  const nextState = {
    ...state,
    toolCalls: nextToolCalls,
    toolOrder: state.toolOrder.includes(id) ? state.toolOrder : [...state.toolOrder, id],
  };
  if (!failed && isImageGenerationTool(payload?.name ?? existing.name)) {
    return addImageArtifact(
      nextState,
      imageArtifactPayloadFromToolResult(id, result, payload?.name ?? existing.name),
    );
  }
  return nextState;
}

function addApproval(
  state: GuiChatState,
  payload: ApprovalPayload | undefined,
): GuiChatState {
  const id = String(payload?.id ?? payload?.request_id ?? "default");
  const approval: ApprovalState = {
    command: payload?.command,
    description: payload?.description,
    id,
    payload: payload ?? {},
    status: "pending",
  };
  return {
    ...state,
    approvalOrder: state.approvalOrder.includes(id)
      ? state.approvalOrder
      : [...state.approvalOrder, id],
    approvals: { ...state.approvals, [id]: approval },
  };
}

function updateApproval(
  state: GuiChatState,
  id: string,
  patch: Partial<ApprovalState>,
): GuiChatState {
  const approval = state.approvals[id];
  if (!approval) return state;
  return {
    ...state,
    approvals: { ...state.approvals, [id]: { ...approval, ...patch } },
  };
}

function resolveApprovalFromEvent(state: GuiChatState, payload: unknown): GuiChatState {
  const id =
    payload && typeof payload === "object"
      ? String(
          (payload as { id?: unknown; request_id?: unknown }).id ??
            (payload as { id?: unknown; request_id?: unknown }).request_id ??
            "",
        )
      : "";
  if (!id) return state;
  return updateApproval(state, id, { status: "approved" });
}

function isImageGenerationTool(name: string | undefined): boolean {
  return name === "image_generate" || name === "image_generation";
}

function imageArtifactPayloadFromToolResult(
  toolCallId: string,
  result: unknown,
  toolName: string,
): ArtifactImagePayload | undefined {
  const record = recordFromUnknown(result);
  if (!record || record.success === false) return undefined;
  const source = firstString(record.host_image, record.image, record.url);
  if (!source) return undefined;
  return {
    id: `${toolCallId}-image`,
    mimeType: mimeTypeForImageSource(source),
    title: toolName === "image_generate" ? "Generated image" : "Image result",
    toolCallId,
    url: imagePreviewUrl(source),
  };
}

function recordFromUnknown(value: unknown): Record<string, unknown> | null {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const parsed: unknown = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function firstString(...values: unknown[]): string | undefined {
  return values.find((value): value is string => typeof value === "string" && value.trim().length > 0);
}

function imagePreviewUrl(source: string): string {
  if (/^(https?:|data:|blob:)/i.test(source) || source.startsWith("/api/")) {
    return source;
  }
  if (looksLikeFilesystemPath(source)) {
    return `/api/fs/read-data-url?path=${encodeURIComponent(source)}`;
  }
  return source;
}

function looksLikeFilesystemPath(source: string): boolean {
  return source.startsWith("~") || /^[A-Za-z]:[\\/]/.test(source) || source.includes("/") || source.includes("\\");
}

function mimeTypeForImageSource(source: string): string | undefined {
  const ext = source.split(/[?#]/, 1)[0]?.split(".").pop()?.toLowerCase();
  switch (ext) {
    case "gif":
      return "image/gif";
    case "jpg":
    case "jpeg":
      return "image/jpeg";
    case "png":
      return "image/png";
    case "svg":
      return "image/svg+xml";
    case "webp":
      return "image/webp";
    default:
      return undefined;
  }
}

function addImageArtifact(
  state: GuiChatState,
  payload: ArtifactImagePayload | undefined,
): GuiChatState {
  const source = payload?.source;
  const rawUrl =
    payload?.url ??
    (typeof source === "string"
      ? source
      : source?.kind === "url" || source?.kind === "artifact"
        ? source.value
        : undefined);
  const id = String(payload?.id ?? rawUrl ?? createClientId("artifact"));
  if (!rawUrl) return state;
  const url =
    rawUrl.startsWith("/api/") ||
    rawUrl.startsWith("http") ||
    rawUrl.startsWith("data:") ||
    rawUrl.startsWith("blob:")
      ? rawUrl
      : looksLikeFilesystemPath(rawUrl)
        ? `/api/fs/read-data-url?path=${encodeURIComponent(rawUrl)}`
        : `/api/artifacts/${encodeURIComponent(rawUrl)}`;
  const messageId = payload?.messageId ?? payload?.message_id;
  const toolCallId = payload?.toolCallId ?? payload?.tool_call_id;
  const artifact: ImageArtifactState = {
    height: payload?.height,
    id,
    messageId,
    mimeType: payload?.mimeType ?? payload?.mime_type,
    title: payload?.title,
    toolCallId,
    url,
    width: payload?.width,
  };
  let messages = state.messages;
  let toolCalls = state.toolCalls;
  if (messageId) {
    messages = messages.map((message) =>
      message.id === messageId
        ? { ...message, artifactIds: appendUnique(message.artifactIds, id) }
        : message,
    );
  } else if (toolCallId && toolCalls[toolCallId]) {
    const tool = toolCalls[toolCallId];
    toolCalls = {
      ...toolCalls,
      [toolCallId]: { ...tool, artifactIds: appendUnique(tool.artifactIds, id) },
    };
  } else if (messages.at(-1)?.role === "assistant") {
    const last = messages.length - 1;
    messages = messages.map((message, index) =>
      index === last ? { ...message, artifactIds: appendUnique(message.artifactIds, id) } : message,
    );
  }
  return { ...state, artifacts: { ...state.artifacts, [id]: artifact }, messages, toolCalls };
}

function applyError(state: GuiChatState, payload: ErrorPayload | undefined): GuiChatState {
  const message = payload?.message ?? "Unknown gateway error";
  const withErrorMessage = appendMessage(state, {
    artifactIds: [],
    id: createClientId("error"),
    role: "system",
    status: "error",
    text: message,
  });
  return { ...withErrorMessage, error: message, isGenerating: false };
}

function appendUnique<T>(items: T[], item: T): T[] {
  return items.includes(item) ? items : [...items, item];
}

function createClientId(prefix: string): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}
