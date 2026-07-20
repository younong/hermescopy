import type { GatewayEvent } from "@/lib/gatewayClient";
import type {
  ApprovalPayload,
  ArtifactFilePayload,
  ArtifactImagePayload,
  ErrorPayload,
  GatewayTranscriptAttachment,
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
import { buildSessionFileDownloadUrl } from "./files";
import { textFromTranscriptMessage } from "./protocol";
import {
  initialGuiChatState,
  type ApprovalState,
  type ArtifactState,
  type ChatMessage,
  type GuiChatConnectionState,
  type GuiChatState,
  type ImageArtifactState,
  type MessageAttachmentState,
  type ToolCallState,
} from "./types";

const MAX_RENDERED_TEXT_CHARS = 120_000;
const RENDERED_TEXT_TRUNCATION_NOTICE =
  "\n\n[… output truncated in Chat GUI to keep the browser responsive …]";
const NON_RENDERED_TOOL_RESULT_NOTICE =
  "[… non-rendered tool result omitted in Chat GUI to keep the browser responsive …]";
const MAX_DISPLAY_MESSAGES = 1_500;
const MAX_DISPLAY_TEXT_CHARS = 16_000_000;
const MAX_DISPLAY_ARTIFACTS = 500;

export type GuiChatAction =
  | { type: "connection"; state: GuiChatConnectionState }
  | { type: "session.created"; response: SessionCreateResponse | SessionResumeResponse }
  | { type: "history.prepend.started" }
  | { type: "history.prepend.succeeded"; response: SessionResumeResponse }
  | { type: "history.prepend.failed"; message: string }
  | { type: "event"; event: GatewayEvent }
  | { type: "user.sent"; attachments?: MessageAttachmentState[]; id: string; text: string }
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
    case "history.prepend.started":
      return { ...state, historyError: undefined, historyLoading: true };
    case "history.prepend.succeeded":
      return prependHistoryPage(state, action.response);
    case "history.prepend.failed":
      return { ...state, historyError: action.message, historyLoading: false };
    case "event":
      return applyGatewayEvent(state, action.event);
    case "user.sent":
      return appendMessage(state, {
        artifactIds: [],
        attachments: action.attachments,
        id: action.id,
        role: "user",
        text: clampRenderedText(action.text),
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
  const cwd = response.info?.cwd ?? (response.session_id === state.sessionId ? state.cwd : undefined);
  const history = Array.isArray(response.messages)
    ? transcriptToHistoryState(response.messages, cwd)
    : null;

  const messages = history ? history.messages : state.messages;
  return {
    ...state,
    artifacts: history ? history.artifacts : state.artifacts,
    cwd,
    error: undefined,
    historyCursor: response.history_page?.cursor ?? undefined,
    historyHasMore: !!response.history_page?.has_more,
    historyLoading: false,
    loadedTextChars: estimateMessageChars(messages),
    safeguardReached: false,
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

function prependHistoryPage(
  state: GuiChatState,
  response: SessionResumeResponse,
): GuiChatState {
  if (response.session_id !== state.sessionId || !Array.isArray(response.messages)) {
    return { ...state, historyLoading: false };
  }
  const history = transcriptToHistoryState(response.messages, state.cwd);
  const knownIds = new Set(state.messages.map((message) => message.id));
  let older = history.messages.filter((message) => !knownIds.has(message.id));
  const remainingMessages = Math.max(0, MAX_DISPLAY_MESSAGES - state.messages.length);
  const remainingChars = Math.max(0, MAX_DISPLAY_TEXT_CHARS - state.loadedTextChars);
  let acceptedChars = 0;
  older = older.slice(-remainingMessages).filter((message) => {
    const size = message.text.length;
    if (acceptedChars + size > remainingChars) return false;
    acceptedChars += size;
    return true;
  });
  const acceptedIds = new Set(older.map((message) => message.id));
  const artifacts = { ...state.artifacts };
  let remainingArtifacts = Math.max(
    0,
    MAX_DISPLAY_ARTIFACTS - countDisplayArtifacts(state.messages, state.artifacts),
  );
  let omittedArtifacts = 0;
  for (const artifact of Object.values(history.artifacts)) {
    if (artifact.messageId && !acceptedIds.has(artifact.messageId)) continue;
    if (artifacts[artifact.id]) continue;
    if (remainingArtifacts <= 0) {
      omittedArtifacts += 1;
      continue;
    }
    artifacts[artifact.id] = artifact;
    remainingArtifacts -= 1;
  }
  const safeguardReached = older.length < history.messages.length || omittedArtifacts > 0;
  return {
    ...state,
    artifacts,
    historyCursor: safeguardReached ? undefined : response.history_page?.cursor ?? undefined,
    historyError: undefined,
    historyHasMore: !safeguardReached && !!response.history_page?.has_more,
    historyLoading: false,
    loadedTextChars: state.loadedTextChars + acceptedChars,
    messages: [...older, ...state.messages],
    safeguardReached,
  };
}

function estimateMessageChars(messages: ChatMessage[]): number {
  return messages.reduce((total, message) => total + message.text.length, 0);
}

function countDisplayArtifacts(
  messages: ChatMessage[],
  artifacts: Record<string, ArtifactState>,
): number {
  const artifactIds = new Set(Object.keys(artifacts));
  let attachmentCount = 0;
  for (const message of messages) attachmentCount += message.attachments?.length ?? 0;
  return artifactIds.size + attachmentCount;
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
      return addImageArtifact(state, event.payload as ArtifactImagePayload | undefined);
    case "artifact.created":
      return addCreatedArtifact(state, event.payload as ArtifactFilePayload | undefined);
    case "error":
      return applyError(state, event.payload as ErrorPayload | undefined);
    default:
      return state;
  }
}

function transcriptToHistoryState(
  transcript: GatewayTranscriptMessage[],
  cwd?: string,
): { artifacts: Record<string, ArtifactState>; messages: ChatMessage[] } {
  const artifacts: Record<string, ArtifactState> = {};
  const messages: ChatMessage[] = [];

  for (const [index, entry] of transcript.entries()) {
    const converted = transcriptToMessageWithArtifacts(entry, index, cwd);
    if (!converted) continue;
    messages.push(converted.message);
    for (const artifact of converted.artifacts) {
      artifacts[artifact.id] = artifact;
    }
  }

  return { artifacts, messages };
}

function transcriptToMessageWithArtifacts(
  message: GatewayTranscriptMessage,
  index: number,
  cwd?: string,
): { artifacts: ArtifactState[]; message: ChatMessage } | null {
  if (message.role === "tool") {
    return null;
  }

  const id = message.id || `history-${index}`;
  const attachments = transcriptAttachments(message.attachments, id, cwd);
  const claimedSources = new Set(
    (message.attachments ?? []).flatMap(attachmentSourcePaths).map(normalizeAttachmentSource),
  );
  const refs = extractImageReferencesFromTranscriptMessage(message).filter(
    (ref) => !claimedSources.has(normalizeAttachmentSource(ref.url)),
  );
  const fileRefs = extractGeneratedFileReferences(textFromTranscriptMessage(message)).filter(
    (ref) =>
      !claimedSources.has(normalizeAttachmentSource(ref.path)) &&
      !refs.some((image) => normalizeAttachmentSource(image.url) === normalizeAttachmentSource(ref.path)),
  );
  const text = clampRenderedText(
    stripAttachmentPromptHints(
      stripRenderableImageReferencesFromText(textFromTranscriptMessage(message), refs),
      message.attachments,
    ),
  );
  if (!text.trim() && refs.length === 0 && fileRefs.length === 0 && attachments.length === 0) {
    return null;
  }

  const artifacts: ArtifactState[] = refs.map((ref, imageIndex): ArtifactState => {
    const url = imagePreviewUrl(ref.url, cwd);
    const name = filenameFromPath(ref.url) || ref.title || "image";
    return {
      downloadUrl: looksLikeFilesystemPath(ref.url)
        ? buildSessionFileDownloadUrl(ref.url, cwd, name)
        : undefined,
      height: ref.height,
      id: `${id}-image-${imageIndex}`,
      messageId: id,
      mimeType: ref.mimeType ?? mimeTypeForImageSource(ref.url),
      title: ref.title || "Historical image",
      url,
      width: ref.width,
    };
  });
  artifacts.push(
    ...fileRefs.map((ref, fileIndex) => ({
      downloadUrl: buildSessionFileDownloadUrl(ref.path, cwd, ref.name),
      id: `${id}-file-${fileIndex}`,
      kind: "file" as const,
      messageId: id,
      mimeType: mimeTypeForFileName(ref.name),
      name: ref.name,
      sourcePath: ref.path,
    })),
  );

  return {
    artifacts,
    message: {
      artifactIds: artifacts.map((artifact) => artifact.id),
      attachments: attachments.length > 0 ? attachments : undefined,
      id,
      role: message.role === "user" ? "user" : message.role === "assistant" ? "assistant" : "system",
      text,
    },
  };
}

function transcriptAttachments(
  values: GatewayTranscriptAttachment[] | undefined,
  messageId: string,
  cwd?: string,
): MessageAttachmentState[] {
  if (!Array.isArray(values)) return [];
  const attachments: MessageAttachmentState[] = [];
  for (const [index, value] of values.entries()) {
    if (!value || typeof value !== "object") continue;
    const kind = value.kind;
    const name = value.name;
    if ((kind !== "image" && kind !== "pdf" && kind !== "file") || typeof name !== "string" || !name) {
      continue;
    }
    const path = typeof value.path === "string" ? value.path : undefined;
    attachments.push({
      downloadUrl: path ? buildSessionFileDownloadUrl(path, cwd, name) : undefined,
      id: `${messageId}-attachment-${index}`,
      kind,
      mimeType: typeof value.mime_type === "string" ? value.mime_type : undefined,
      name,
      pagesAttached: finiteNonNegativeNumber(value.pages_attached),
      previewUrl: kind === "image" && path ? imagePreviewUrl(path, cwd) : undefined,
      refText: typeof value.ref_text === "string" ? value.ref_text : undefined,
      sizeBytes: finiteNonNegativeNumber(value.size_bytes) ?? 0,
      sourcePath: path,
    });
  }
  return attachments;
}

function finiteNonNegativeNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : undefined;
}

function attachmentSourcePaths(value: GatewayTranscriptAttachment): string[] {
  const paths: string[] = [];
  if (typeof value.path === "string") paths.push(value.path);
  if (Array.isArray(value.source_paths)) {
    paths.push(...value.source_paths.filter((path): path is string => typeof path === "string"));
  }
  return paths;
}

function normalizeAttachmentSource(value: string): string {
  return value.replace(/^file:\/\//i, "");
}

function stripAttachmentPromptHints(
  text: string,
  values: GatewayTranscriptAttachment[] | undefined,
): string {
  if (!text || !Array.isArray(values) || values.length === 0) return text;
  const sourcePaths = new Set(values.flatMap(attachmentSourcePaths).map(normalizeAttachmentSource));
  const refTexts = new Set(
    values
      .map((value) => value.ref_text)
      .filter((value): value is string => typeof value === "string" && value.length > 0),
  );
  const lines = text.split("\n").filter((line) => {
    const trimmed = line.trim();
    const hint = trimmed.match(/^\[Image attached at:\s*(.+)]$/);
    if (hint && sourcePaths.has(normalizeAttachmentSource(hint[1].trim()))) return false;
    if (sourcePaths.has(normalizeAttachmentSource(trimmed))) return false;
    if (refTexts.has(trimmed)) return false;
    if (trimmed === "附件：" && refTexts.size > 0) return false;
    return true;
  });
  return lines.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

interface ExtractedImageReference {
  end?: number;
  height?: number;
  mimeType?: string;
  source: "markdown" | "structured" | "url";
  start?: number;
  title?: string;
  url: string;
  width?: number;
}

function extractImageReferencesFromTranscriptMessage(
  message: GatewayTranscriptMessage,
): ExtractedImageReference[] {
  const text = typeof message.text === "string" ? message.text : "";
  const contentRefs = extractImageReferencesFromContent(message.content);
  const textRefs = extractImageReferencesFromText(text).filter(
    (ref) => contentRefs.length === 0 || !isNativeImageAttachmentHintReference(text, ref),
  );

  return dedupeImageReferences([...textRefs, ...contentRefs]);
}

function extractImageReferencesFromContent(content: unknown): ExtractedImageReference[] {
  if (typeof content === "string") {
    return extractImageReferencesFromText(content);
  }
  if (Array.isArray(content)) {
    return dedupeImageReferences(content.flatMap(extractImageReferencesFromContent));
  }
  if (!content || typeof content !== "object") {
    return [];
  }

  const record = content as Record<string, unknown>;
  const type = typeof record.type === "string" ? record.type : "";
  const imageUrl = imageUrlFromUnknown(record.image_url);
  const sourceUrl = imageUrlFromUnknown(record.source);
  const directUrl = firstString(record.url, record.image, record.input_image);
  const candidate = imageUrl ?? sourceUrl ?? directUrl;
  const refs: ExtractedImageReference[] = [];

  if (candidate && isImageContentType(type) && isLikelyImageReference(candidate)) {
    refs.push({
      height: numberFromUnknown(record.height),
      mimeType: firstString(record.mimeType, record.mime_type) ?? mimeTypeForImageSource(candidate),
      source: "structured",
      title: firstString(record.title, record.name, record.alt),
      url: candidate,
      width: numberFromUnknown(record.width),
    });
  }

  for (const key of ["content", "parts", "items"] as const) {
    if (Array.isArray(record[key])) {
      refs.push(...extractImageReferencesFromContent(record[key]));
    }
  }

  return dedupeImageReferences(refs);
}

function imageUrlFromUnknown(value: unknown): string | undefined {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return undefined;
  const record = value as Record<string, unknown>;
  return firstString(record.url, record.value, record.path);
}

function isNativeImageAttachmentHintReference(text: string, ref: ExtractedImageReference): boolean {
  if (ref.source !== "url" || ref.start === undefined || ref.end === undefined) return false;
  const lineStart = text.lastIndexOf("\n", ref.start) + 1;
  const nextNewline = text.indexOf("\n", ref.end);
  const lineEnd = nextNewline < 0 ? text.length : nextNewline;
  const line = text.slice(lineStart, lineEnd).trim();
  return isNativeImageAttachmentHintLine(line);
}

function isImageContentType(type: string): boolean {
  return type === "image" || type === "image_url" || type === "input_image" || type === "artifact.image";
}

function numberFromUnknown(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function extractImageReferencesFromText(text: string): ExtractedImageReference[] {
  if (!text) return [];
  const codeRanges = rangesForFencedCodeBlocks(text);
  return dedupeImageReferences([
    ...extractMarkdownImageReferences(text, codeRanges),
    ...extractMarkdownLinkImageReferences(text, codeRanges),
    ...extractBareImageReferences(text, codeRanges),
  ]);
}

function extractMarkdownImageReferences(
  text: string,
  codeRanges: Array<{ end: number; start: number }>,
): ExtractedImageReference[] {
  return extractMarkdownReferences(text, codeRanges, true);
}

function extractMarkdownLinkImageReferences(
  text: string,
  codeRanges: Array<{ end: number; start: number }>,
): ExtractedImageReference[] {
  return extractMarkdownReferences(text, codeRanges, false);
}

function extractMarkdownReferences(
  text: string,
  codeRanges: Array<{ end: number; start: number }>,
  imageOnly: boolean,
): ExtractedImageReference[] {
  const refs: ExtractedImageReference[] = [];
  let cursor = 0;
  while (cursor < text.length) {
    const marker = imageOnly ? "![" : "[";
    const start = text.indexOf(marker, cursor);
    if (start < 0) break;
    if (!imageOnly && start > 0 && text[start - 1] === "!") {
      cursor = start + 1;
      continue;
    }
    if (isIndexInRanges(start, codeRanges)) {
      cursor = start + marker.length;
      continue;
    }
    const labelStart = start + marker.length;
    const labelEnd = text.indexOf("](", labelStart);
    if (labelEnd < 0) break;
    const parsed = parseMarkdownDestination(text, labelEnd + 2);
    if (!parsed) {
      cursor = labelEnd + 2;
      continue;
    }
    const url = normalizeExtractedImageReference(parsed.destination);
    if (url && isLikelyImageReference(url)) {
      refs.push({
        end: parsed.end,
        source: imageOnly ? "markdown" : "url",
        start,
        title: text.slice(labelStart, labelEnd).trim() || undefined,
        url,
      });
    }
    cursor = parsed.end;
  }
  return refs;
}

function parseMarkdownDestination(
  text: string,
  start: number,
): { destination: string; end: number } | null {
  let index = start;
  while (index < text.length && /\s/.test(text[index])) index += 1;
  if (text[index] === "<") {
    const close = text.indexOf(">", index + 1);
    if (close < 0) return null;
    const paren = text.indexOf(")", close + 1);
    if (paren < 0) return null;
    return { destination: text.slice(index + 1, close), end: paren + 1 };
  }

  let depth = 0;
  for (; index < text.length; index += 1) {
    const ch = text[index];
    if (ch === "(") depth += 1;
    if (ch === ")") {
      if (depth === 0) {
        const raw = text.slice(start, index).trim().replace(/\s+"[^"]*"\s*$/, "");
        return { destination: raw, end: index + 1 };
      }
      depth -= 1;
    }
  }
  return null;
}

function extractBareImageReferences(
  text: string,
  codeRanges: Array<{ end: number; start: number }>,
): ExtractedImageReference[] {
  const refs: ExtractedImageReference[] = [];
  const bareReferenceRe = new RegExp(
    String.raw`(?:https?:\/\/[^\s<>()]+|file:\/\/[^\n<>()]+|sandbox:\/{0,2}[^\n<>()]+|data:image\/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]+|\/api\/(?:fs\/read-data-url|artifacts)\?[^\s<>()]+|\/api\/artifacts\/[^\s<>()]+|(?:~|\.{1,2})\/[^\n<>()]+?\.(?:png|jpe?g|gif|webp|svg|avif|bmp|ico)(?:[?#][^\s<>()]+)?|\/[^\n<>()]+?\.(?:png|jpe?g|gif|webp|svg|avif|bmp|ico)(?:[?#][^\s<>()]+)?|[A-Za-z]:[\\/][^\n<>()]+?\.(?:png|jpe?g|gif|webp|svg|avif|bmp|ico)(?:[?#][^\s<>()]+)?|(?:[^\s<>()]+\/)+[^\n<>()]+?\.(?:png|jpe?g|gif|webp|svg|avif|bmp|ico)(?:[?#][^\s<>()]+)?)`,
    "gi",
  );
  for (const match of text.matchAll(bareReferenceRe)) {
    const start = match.index ?? 0;
    if (isIndexInRanges(start, codeRanges)) continue;
    const url = normalizeExtractedImageReference(match[0]);
    if (!url || !isLikelyImageReference(url)) continue;
    refs.push({
      end: start + match[0].length,
      source: "url",
      start,
      url,
    });
  }
  return refs;
}

interface ExtractedFileReference {
  name: string;
  path: string;
}

function extractGeneratedFileReferences(text: string): ExtractedFileReference[] {
  const codeRanges = rangesForFencedCodeBlocks(text);
  const candidates: Array<{ index: number; path: string }> = [];
  const savedPattern = /(?:Full output saved to:|Full text saved to:)\s*([^\n]+)/gi;
  for (const match of text.matchAll(savedPattern)) {
    candidates.push({ index: match.index ?? 0, path: match[1].trim() });
  }
  const markdownLinkPattern = /(?<!!)\[[^\]]+]\(([^)\n]+)\)/g;
  for (const match of text.matchAll(markdownLinkPattern)) {
    candidates.push({ index: match.index ?? 0, path: match[1].trim().replace(/^<|>$/g, "") });
  }
  const labeledInlinePathPattern = /(?:文件路径|文件地址|file\s*path|saved\s*(?:file\s*)?(?:at|to))\s*[：:]\s*(?:\*{1,2})?\s*`([^`\n]+)`/gi;
  for (const match of text.matchAll(labeledInlinePathPattern)) {
    candidates.push({ index: match.index ?? 0, path: match[1].trim() });
  }

  const seen = new Set<string>();
  const refs: ExtractedFileReference[] = [];
  for (const candidate of candidates) {
    if (isIndexInRanges(candidate.index, codeRanges)) continue;
    const path = normalizeGeneratedFilePath(candidate.path);
    if (!path || seen.has(path)) continue;
    const name = filenameFromPath(path);
    if (!name) continue;
    seen.add(path);
    refs.push({ name, path });
  }
  return refs;
}

function normalizeGeneratedFilePath(value: string): string | null {
  let path = value.trim().replace(/[.,;:!?。，、；：！？]+$/g, "");
  const sandbox = path.match(/^sandbox:\/{0,2}(\/.*)$/i);
  if (sandbox?.[1]) path = sandbox[1];
  if (/^[a-z][a-z0-9+.-]*:/i.test(path) && !/^file:/i.test(path)) return null;
  if (/^file:/i.test(path)) {
    try {
      const parsed = new URL(path);
      if (parsed.hostname && parsed.hostname !== "localhost") return null;
      path = decodeURIComponent(parsed.pathname);
    } catch {
      return null;
    }
  }
  return path && !path.includes("\0") ? path : null;
}

function filenameFromPath(path: string): string {
  const clean = path.split(/[?#]/, 1)[0] ?? path;
  return clean.split(/[\\/]/).filter(Boolean).at(-1) ?? "";
}

function mimeTypeForFileName(name: string): string | undefined {
  const extension = name.split(".").pop()?.toLowerCase();
  const known: Record<string, string> = {
    csv: "text/csv",
    html: "text/html",
    htm: "text/html",
    json: "application/json",
    md: "text/markdown",
    pdf: "application/pdf",
    txt: "text/plain",
    zip: "application/zip",
  };
  return extension ? known[extension] : undefined;
}

function normalizeExtractedImageReference(value: string): string | null {
  const trimmed = trimImageReferenceBoundary(value);
  if (!trimmed) return null;
  const sandbox = trimmed.match(/^sandbox:\/{0,2}(\/.*)$/i);
  if (sandbox?.[1] && IMAGE_EXTENSION_RE.test(sandbox[1])) {
    return sandbox[1];
  }
  return trimmed;
}

function trimImageReferenceBoundary(value: string): string {
  let next = value.trim();
  next = next.replace(/^["'“‘「『《（([{<]+/g, "");
  next = next.replace(/[.,;:!?。，、；：！？]+$/g, "");
  next = trimUnbalancedClosingDelimiters(next);
  return next;
}

function trimUnbalancedClosingDelimiters(value: string): string {
  let next = value;
  const pairs: Array<[string, string]> = [
    ["(", ")"],
    ["[", "]"],
    ["{", "}"],
    ["<", ">"],
    ["（", "）"],
    ["【", "】"],
    ["《", "》"],
    ["“", "”"],
    ["‘", "’"],
    ["「", "」"],
    ["『", "』"],
  ];
  let changed = true;
  while (changed && next) {
    changed = false;
    for (const [open, close] of pairs) {
      if (!next.endsWith(close)) continue;
      const opens = countChar(next, open);
      const closes = countChar(next, close);
      if (closes > opens) {
        next = next.slice(0, -close.length).trimEnd();
        changed = true;
      }
    }
  }
  return next;
}

function countChar(value: string, char: string): number {
  return Array.from(value).filter((candidate) => candidate === char).length;
}

function dedupeImageReferences(refs: ExtractedImageReference[]): ExtractedImageReference[] {
  const seen = new Set<string>();
  const deduped: ExtractedImageReference[] = [];
  for (const ref of refs) {
    const key = ref.url;
    if (!key || seen.has(key)) continue;
    seen.add(key);
    deduped.push(ref);
  }
  return deduped;
}

function isLikelyImageReference(value: string): boolean {
  const url = value.trim();
  if (!url) return false;
  if (/^(?:javascript|mailto):/i.test(url)) return false;
  if (/^data:image\/[a-z0-9.+-]+;base64,/i.test(url)) return true;
  if (/^data:/i.test(url)) return false;
  if (url.startsWith("/api/fs/read-data-url?") || url.startsWith("/api/artifacts/")) {
    return true;
  }
  if (isBareFilename(url)) return false;
  return IMAGE_EXTENSION_RE.test(url);
}

function isBareFilename(value: string): boolean {
  return /^[^\\/]+\.(?:png|jpe?g|gif|webp|svg|avif|bmp|ico)(?:[?#].*)?$/i.test(value);
}

const IMAGE_EXTENSION_RE = /\.(?:png|jpe?g|gif|webp|svg|avif|bmp|ico)(?=$|[\s?#&=/%]|[。。，、；：！？)\]}）】》]|$)/i;

function isNativeImageAttachmentHintLine(line: string): boolean {
  return line.startsWith("[Image attached at:") && line.endsWith("]");
}

function stripRenderableImageReferencesFromText(
  text: string,
  refs: ExtractedImageReference[],
): string {
  if (!text || refs.length === 0) return text;
  const markdownRanges = refs
    .filter((ref) => ref.source === "markdown" && ref.start !== undefined && ref.end !== undefined)
    .sort((a, b) => (b.start ?? 0) - (a.start ?? 0));
  let next = text;
  for (const ref of markdownRanges) {
    next = `${next.slice(0, ref.start)}${next.slice(ref.end)}`;
  }

  const standaloneUrls = new Set(refs.filter((ref) => ref.source === "url").map((ref) => ref.url));
  const hasStructuredImage = refs.some((ref) => ref.source === "structured");
  next = next
    .split("\n")
    .filter((line) => {
      const trimmed = line.trim();
      if (standaloneUrls.has(trimImageReferenceBoundary(trimmed))) return false;
      if (hasStructuredImage && isNativeImageAttachmentHintLine(trimmed)) return false;
      return true;
    })
    .join("\n");

  return next.replace(/\n{3,}/g, "\n\n").trim();
}

function rangesForFencedCodeBlocks(text: string): Array<{ end: number; start: number }> {
  const ranges: Array<{ end: number; start: number }> = [];
  const fenceRe = /```[\s\S]*?```/g;
  for (const match of text.matchAll(fenceRe)) {
    const start = match.index ?? 0;
    ranges.push({ end: start + match[0].length, start });
  }
  return ranges;
}

function isIndexInRanges(index: number, ranges: Array<{ end: number; start: number }>): boolean {
  return ranges.some((range) => index >= range.start && index < range.end);
}

function appendRenderedText(current: string, delta: string): string {
  if (!delta || current.endsWith(RENDERED_TEXT_TRUNCATION_NOTICE)) return current;
  return clampRenderedText(`${current}${delta}`);
}

function clampRenderedText(text: string): string {
  if (text.length <= MAX_RENDERED_TEXT_CHARS) return text;
  return `${text.slice(0, MAX_RENDERED_TEXT_CHARS)}${RENDERED_TEXT_TRUNCATION_NOTICE}`;
}

function clampToolInput(input: unknown): unknown {
  if (input === undefined || input === null) return input;
  if (typeof input === "string") return clampRenderedText(input);
  if (typeof input === "number" || typeof input === "boolean") return input;
  return NON_RENDERED_TOOL_RESULT_NOTICE;
}

function preserveToolResult(name: string | undefined): boolean {
  return isImageGenerationTool(name);
}

function appendMessage(state: GuiChatState, message: ChatMessage): GuiChatState {
  const messages = [...state.messages, message];
  const nextChars = state.loadedTextChars + message.text.length;
  if (
    messages.length <= MAX_DISPLAY_MESSAGES
    && nextChars <= MAX_DISPLAY_TEXT_CHARS
    && countDisplayArtifacts(messages, state.artifacts) <= MAX_DISPLAY_ARTIFACTS
  ) {
    return { ...state, loadedTextChars: nextChars, messages };
  }

  const protectedIds = new Set(
    messages
      .filter((value) => value.streaming)
      .map((value) => value.id),
  );
  let loadedTextChars = nextChars;
  while (
    messages.length > 1
    && (
      messages.length > MAX_DISPLAY_MESSAGES
      || loadedTextChars > MAX_DISPLAY_TEXT_CHARS
      || countDisplayArtifacts(messages, state.artifacts) > MAX_DISPLAY_ARTIFACTS
    )
  ) {
    const removable = messages.findIndex((value) => !protectedIds.has(value.id));
    if (removable < 0) break;
    loadedTextChars -= messages[removable].text.length;
    messages.splice(removable, 1);
  }
  const liveArtifactIds = new Set(messages.flatMap((value) => value.artifactIds));
  const artifacts = Object.fromEntries(
    Object.entries(state.artifacts).filter(([id, artifact]) =>
      liveArtifactIds.has(id) || (artifact.toolCallId && state.toolCalls[artifact.toolCallId]),
    ),
  );
  return {
    ...state,
    artifacts,
    historyCursor: undefined,
    historyHasMore: false,
    loadedTextChars,
    messages,
    safeguardReached: true,
  };
}

function applySessionInfo(
  state: GuiChatState,
  payload: SessionInfoPayload | undefined,
): GuiChatState {
  if (!payload) return state;
  return { ...state, cwd: payload.cwd ?? state.cwd, model: payload.model ?? state.model };
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
    i === idx
      ? { ...message, streaming: true, text: appendRenderedText(message.text, delta) }
      : message,
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
          text:
            finalText !== undefined && finalText !== message.text
              ? clampRenderedText(finalText)
              : message.text,
        }
      : message,
  );
  const statusLines = payload?.warning
    ? [...working.statusLines, payload.warning].slice(-8)
    : working.statusLines;
  return addGeneratedFileArtifacts(
    { ...working, isGenerating: false, messages, statusLines },
    messages[idx]?.text ?? "",
    { messageId: messages[idx]?.id },
  );
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
    argsText: payload?.args_text ? clampRenderedText(payload.args_text) : existing?.argsText,
    id,
    input: clampToolInput(payload?.input ?? payload?.context ?? existing?.input),
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
      [id]: { ...tool, output: appendRenderedText(tool.output, `${tool.output ? "\n" : ""}${text}`) },
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
  const output = clampRenderedText(payload?.result_text ?? payload?.output ?? existing.output);
  const toolName = payload?.name ?? existing.name;
  const result = preserveToolResult(toolName) ? payload?.result ?? existing.result : existing.result;
  const nextTool: ToolCallState = {
    ...existing,
    durationSeconds: payload?.duration_s ?? existing.durationSeconds,
    error: payload?.error ? clampRenderedText(payload.error) : existing.error,
    name: toolName,
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
  if (!failed && isImageGenerationTool(toolName)) {
    return addImageArtifact(
      nextState,
      imageArtifactPayloadFromToolResult(id, result, toolName),
    );
  }
  return failed ? nextState : addGeneratedFileArtifacts(nextState, output, { toolCallId: id });
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
    url: source,
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

function imagePreviewUrl(source: string, cwd?: string): string {
  if (/^(https?:|data:|blob:)/i.test(source) || source.startsWith("/api/")) {
    return source;
  }
  const generatedImageUrl = generatedImagePreviewUrl(source);
  if (generatedImageUrl) return generatedImageUrl;
  if (looksLikeFilesystemPath(source)) {
    const pathParam = `path=${encodeURIComponent(source)}`;
    const cwdParam = cwd && isRelativeFilesystemPath(source) ? `&cwd=${encodeURIComponent(cwd)}` : "";
    return `/api/fs/read-data-url?${pathParam}${cwdParam}`;
  }
  return source;
}

function generatedImagePreviewUrl(source: string): string | null {
  const path = source.split(/[?#]/, 1)[0] ?? source;
  const match = path.match(/(?:^|[/\\])\.hermes[/\\](?:images|cache[/\\]images)[/\\]([^/\\]+)$/);
  if (!match?.[1]) return null;
  return `/api/generated-images/${encodeURIComponent(match[1])}`;
}

function looksLikeFilesystemPath(source: string): boolean {
  return source.startsWith("~") || /^[A-Za-z]:[\\/]/.test(source) || source.includes("/") || source.includes("\\");
}

function isRelativeFilesystemPath(source: string): boolean {
  return (
    !source.startsWith("~") &&
    !source.startsWith("/") &&
    !source.startsWith("\\\\") &&
    !/^file:/i.test(source) &&
    !/^[A-Za-z]:[\\/]/.test(source)
  );
}

function mimeTypeForImageSource(source: string): string | undefined {
  const dataUrlMatch = source.match(/^data:([^;,]+)[;,]/i);
  if (dataUrlMatch?.[1]?.toLowerCase().startsWith("image/")) {
    return dataUrlMatch[1].toLowerCase();
  }
  const ext = source.split(/[?#]/, 1)[0]?.split(".").pop()?.toLowerCase();
  switch (ext) {
    case "avif":
      return "image/avif";
    case "bmp":
      return "image/bmp";
    case "gif":
      return "image/gif";
    case "ico":
      return "image/x-icon";
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

function addGeneratedFileArtifacts(
  state: GuiChatState,
  text: string,
  owner: { messageId?: string; toolCallId?: string },
): GuiChatState {
  let next = state;
  for (const [index, ref] of extractGeneratedFileReferences(text).entries()) {
    const id = `${owner.messageId ?? owner.toolCallId ?? "artifact"}-file-${index}`;
    if (next.artifacts[id]) continue;
    const artifact: ArtifactState = {
      downloadUrl: buildSessionFileDownloadUrl(ref.path, next.cwd, ref.name),
      id,
      kind: "file",
      messageId: owner.messageId,
      mimeType: mimeTypeForFileName(ref.name),
      name: ref.name,
      sourcePath: ref.path,
      toolCallId: owner.toolCallId,
    };
    const messages = owner.messageId
      ? next.messages.map((message) =>
          message.id === owner.messageId
            ? { ...message, artifactIds: appendUnique(message.artifactIds, id) }
            : message,
        )
      : next.messages;
    const toolCalls = owner.toolCallId && next.toolCalls[owner.toolCallId]
      ? {
          ...next.toolCalls,
          [owner.toolCallId]: {
            ...next.toolCalls[owner.toolCallId],
            artifactIds: appendUnique(next.toolCalls[owner.toolCallId].artifactIds, id),
          },
        }
      : next.toolCalls;
    next = {
      ...next,
      artifacts: { ...next.artifacts, [id]: artifact },
      messages,
      toolCalls,
    };
  }
  return next;
}

function addCreatedArtifact(
  state: GuiChatState,
  payload: ArtifactFilePayload | undefined,
): GuiChatState {
  const source = payload?.source;
  const sourcePath =
    payload?.path ??
    payload?.url ??
    (typeof source === "string" ? source : source?.value);
  if (!sourcePath) return state;
  const mimeType = payload?.mimeType ?? payload?.mime_type;
  if (mimeType?.toLowerCase().startsWith("image/") || isLikelyImageReference(sourcePath)) {
    return addImageArtifact(state, payload);
  }

  const id = String(payload?.id ?? sourcePath ?? createClientId("artifact"));
  const name = payload?.name ?? payload?.filename ?? payload?.title ?? filenameFromPath(sourcePath);
  if (!name) return state;
  const messageId = payload?.messageId ?? payload?.message_id;
  const toolCallId = payload?.toolCallId ?? payload?.tool_call_id;
  const artifact: ArtifactState = {
    downloadUrl: buildSessionFileDownloadUrl(sourcePath, state.cwd, name),
    id,
    kind: "file",
    messageId,
    mimeType: mimeType ?? mimeTypeForFileName(name),
    name,
    sourcePath,
    toolCallId,
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

function addImageArtifact(
  state: GuiChatState,
  payload: ArtifactImagePayload | undefined,
): GuiChatState {
  const source = payload?.source;
  const rawUrl =
    payload?.path ??
    payload?.url ??
    (typeof source === "string"
      ? source
      : source?.kind === "url" || source?.kind === "artifact"
        ? source.value
        : undefined);
  const id = String(payload?.id ?? rawUrl ?? createClientId("artifact"));
  if (!rawUrl) return state;
  const url = looksLikeFilesystemPath(rawUrl)
    ? imagePreviewUrl(rawUrl, state.cwd)
    : rawUrl.startsWith("/api/") ||
        rawUrl.startsWith("http") ||
        rawUrl.startsWith("data:") ||
        rawUrl.startsWith("blob:")
      ? rawUrl
      : `/api/artifacts/${encodeURIComponent(rawUrl)}`;
  const messageId = payload?.messageId ?? payload?.message_id;
  const toolCallId = payload?.toolCallId ?? payload?.tool_call_id;
  const name = filenameFromPath(rawUrl) || payload?.title || "image";
  const artifact: ImageArtifactState = {
    downloadUrl: looksLikeFilesystemPath(rawUrl)
      ? buildSessionFileDownloadUrl(rawUrl, state.cwd, name)
      : undefined,
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
