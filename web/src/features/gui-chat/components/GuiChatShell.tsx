import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { AlertCircle, PanelRight, RefreshCw, Terminal, X } from "lucide-react";
import { ChatSessionList } from "@/components/ChatSessionList";
import { usePageHeader } from "@/contexts/usePageHeader";
import { useProfileScope } from "@/contexts/useProfileScope";
import { useI18n } from "@/i18n";
import type { GatewayEvent } from "@/lib/gatewayClient";
import { dashboardAuthTransition } from "@/lib/dashboardAuthTransition";
import { useDashboardAuthIdentity } from "@/lib/useDashboardAuthIdentity";
import { cn } from "@/lib/utils";
import { connectGuiChat, type GuiChatConnection } from "../api";
import { connectMockGuiChat } from "../mock";
import { guiChatReducer } from "../reducer";
import {
  initialGuiChatState,
  type GuiComposerAttachment,
  type MessageAttachmentState,
} from "../types";
import { Composer } from "./Composer";
import { MessageList } from "./MessageList";

export function GuiChatShell() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { setEnd, setTitle } = usePageHeader();
  const { profile } = useProfileScope();
  const [searchParams, setSearchParams] = useSearchParams();
  const resumeSessionId = searchParams.get("resume");
  const mockMode = searchParams.get("mock") === "1";
  const [state, dispatch] = useReducer(guiChatReducer, initialGuiChatState);
  const connectionRef = useRef<GuiChatConnection | null>(null);
  const pendingStreamEventsRef = useRef<GatewayEvent[]>([]);
  const streamFlushFrameRef = useRef<number | undefined>(undefined);
  const [newChatNonce, setNewChatNonce] = useState(0);
  const [sendScrollNonce, setSendScrollNonce] = useState(0);
  const [mobilePanelOpenRaw, setMobilePanelOpenRaw] = useState(false);
  const { ownerKey, ready: authIdentityReady } = useDashboardAuthIdentity();
  const [portalRoot] = useState<HTMLElement | null>(() =>
    typeof document !== "undefined" ? document.body : null,
  );
  const [narrow, setNarrow] = useState(() =>
    typeof window !== "undefined"
      ? window.matchMedia("(max-width: 1023px)").matches
      : false,
  );
  const mobilePanelOpen = mobilePanelOpenRaw;
  const activeSessionId = state.storedSessionId ?? resumeSessionId;
  const terminalResumeId = state.storedSessionId ?? resumeSessionId;
  const forceBottomKey = `${activeSessionId ?? `new-${newChatNonce}`}:${sendScrollNonce}`;
  const closeMobilePanel = useCallback(() => setMobilePanelOpenRaw(false), []);

  useEffect(() => dashboardAuthTransition.register(() => {
    connectionRef.current?.close();
    connectionRef.current = null;
    if (streamFlushFrameRef.current !== undefined) {
      cancelAnimationFrame(streamFlushFrameRef.current);
      streamFlushFrameRef.current = undefined;
    }
    pendingStreamEventsRef.current = [];
    dispatch({ type: "reset" });
  }), []);

  const flushStreamEvents = useCallback(() => {
    streamFlushFrameRef.current = undefined;
    const events = mergeBufferedStreamEvents(pendingStreamEventsRef.current);
    pendingStreamEventsRef.current = [];
    for (const event of events) {
      dispatch({ type: "event", event });
    }
  }, []);

  const dispatchGatewayEvent = useCallback(
    (event: GatewayEvent) => {
      if (isBufferedStreamEvent(event)) {
        pendingStreamEventsRef.current.push(event);
        if (streamFlushFrameRef.current === undefined) {
          streamFlushFrameRef.current = requestAnimationFrame(flushStreamEvents);
        }
        return;
      }

      if (streamFlushFrameRef.current !== undefined) {
        cancelAnimationFrame(streamFlushFrameRef.current);
        streamFlushFrameRef.current = undefined;
      }
      flushStreamEvents();
      dispatch({ type: "event", event });
    },
    [flushStreamEvents],
  );

  const startNewGuiChat = useCallback(() => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete("resume");
        return next;
      },
      { replace: true },
    );
    setNewChatNonce((n) => n + 1);
  }, [setSearchParams]);

  const connect = useCallback(() => {
    connectionRef.current?.close();
    if (streamFlushFrameRef.current !== undefined) {
      cancelAnimationFrame(streamFlushFrameRef.current);
      streamFlushFrameRef.current = undefined;
    }
    pendingStreamEventsRef.current = [];
    dispatch({ type: "reset" });
    const connection = mockMode
      ? connectMockGuiChat()
      : connectGuiChat({ ownerKey, profile, resumeSessionId });
    connectionRef.current = connection;
    const offState = connection.client.onState((next) => {
      dispatch({ type: "connection", state: next });
    });
    const offEvents = connection.client.onEvent(dispatchGatewayEvent);
    void connection
      .createOrResume()
      .then((response) => dispatch({ type: "session.created", response }))
      .catch((error: Error) => dispatch({ type: "error", message: error.message }));
    return () => {
      offState();
      offEvents();
      if (streamFlushFrameRef.current !== undefined) {
        cancelAnimationFrame(streamFlushFrameRef.current);
        streamFlushFrameRef.current = undefined;
      }
      pendingStreamEventsRef.current = [];
      connection.close();
      if (connectionRef.current === connection) {
        connectionRef.current = null;
      }
    };
  }, [dispatchGatewayEvent, mockMode, ownerKey, profile, resumeSessionId]);

  useEffect(() => {
    if (!authIdentityReady) return;
    return connect();
  }, [authIdentityReady, connect, newChatNonce]);

  useEffect(() => {
    const mql = window.matchMedia("(max-width: 1023px)");
    const sync = () => setNarrow(mql.matches);
    sync();
    mql.addEventListener("change", sync);
    return () => mql.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    if (!mobilePanelOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeMobilePanel();
    };
    document.addEventListener("keydown", onKey);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [mobilePanelOpen, closeMobilePanel]);

  useEffect(() => {
    const mql = window.matchMedia("(min-width: 1024px)");
    const onChange = (e: MediaQueryListEvent) => {
      if (e.matches) setMobilePanelOpenRaw(false);
    };
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    setTitle("Chat GUI (beta)");
    setEnd(
      <div className="flex items-center gap-2">
        {narrow ? (
          <Button
            ghost
            size="sm"
            onClick={() => setMobilePanelOpenRaw(true)}
            aria-expanded={mobilePanelOpen}
            aria-controls="gui-chat-session-panel"
          >
            <PanelRight className="h-4 w-4" />
            Sessions
          </Button>
        ) : null}
        <Button
          ghost
          size="sm"
          onClick={() =>
            navigate(
              terminalResumeId
                ? `/chat?resume=${encodeURIComponent(terminalResumeId)}`
                : "/chat",
            )
          }
        >
          <Terminal className="h-4 w-4" />
          Terminal Chat
        </Button>
      </div>,
    );
    return () => {
      setTitle(null);
      setEnd(null);
    };
  }, [mobilePanelOpen, narrow, navigate, setEnd, setTitle, terminalResumeId]);

  const disabled = state.connection !== "open" || !state.sessionId;
  const statusTone = useMemo(() => {
    if (state.connection === "open") return "success";
    if (state.connection === "error") return "destructive";
    if (state.connection === "connecting") return "warning";
    return "secondary";
  }, [state.connection]);

  const send = useCallback(
    async (
      text: string,
      attachments: GuiComposerAttachment[],
      updateAttachment: (id: string, patch: Partial<GuiComposerAttachment>) => void,
    ) => {
      const sessionId = state.sessionId;
      const connection = connectionRef.current;
      if (!sessionId || !connection) return;

      try {
        const messageAttachments: MessageAttachmentState[] = [];
        const fileRefs: string[] = [];

        for (const attachment of attachments) {
          let sentAttachment = attachment;
          if (attachment.status === "uploaded" && attachment.stagedSessionId === sessionId) {
            messageAttachments.push(toMessageAttachment(sentAttachment));
            if (attachment.kind === "file" && attachment.refText) fileRefs.push(attachment.refText);
            continue;
          }

          updateAttachment(attachment.id, { error: undefined, status: "uploading" });
          try {
            if (attachment.kind === "image") {
              const result = await connection.attachImage(sessionId, attachment.file);
              if (!result.attached) {
                throw new Error(result.message || `Could not attach ${attachment.name}`);
              }
              sentAttachment = {
                ...attachment,
                attachedPath: result.path,
                error: undefined,
                stagedSessionId: sessionId,
                status: "uploaded",
              };
              updateAttachment(attachment.id, sentAttachment);
            } else if (attachment.kind === "pdf") {
              const result = await connection.attachPdf(sessionId, attachment.file);
              if (!result.attached) {
                throw new Error(result.message || `Could not attach ${attachment.name}`);
              }
              sentAttachment = {
                ...attachment,
                error: undefined,
                pagesAttached: result.pages_attached,
                stagedSessionId: sessionId,
                status: "uploaded",
              };
              updateAttachment(attachment.id, sentAttachment);
            } else {
              const result = await connection.attachFile(sessionId, attachment.file);
              if (!result.attached || !result.ref_text) {
                throw new Error(result.message || `Could not attach ${attachment.name}`);
              }
              sentAttachment = {
                ...attachment,
                attachedPath: result.path,
                error: undefined,
                refText: result.ref_text,
                stagedSessionId: sessionId,
                status: "uploaded",
              };
              fileRefs.push(result.ref_text);
              updateAttachment(attachment.id, sentAttachment);
            }
          } catch (error) {
            const message = error instanceof Error ? error.message : String(error);
            throw new AttachmentError(attachment.id, message);
          }
          messageAttachments.push(toMessageAttachment(sentAttachment));
        }

        const promptText = appendFileReferences(text, fileRefs);
        setSendScrollNonce((n) => n + 1);
        dispatch({
          type: "user.sent",
          attachments: messageAttachments,
          id: createClientId("user"),
          text,
        });
        await connection.send(sessionId, promptText);
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        if (error instanceof AttachmentError) {
          updateAttachment(error.attachmentId, { error: message, status: "error" });
        }
        dispatch({ type: "error", message });
        throw error;
      }
    },
    [state.sessionId],
  );

  const stop = useCallback(() => {
    const sessionId = state.sessionId;
    const connection = connectionRef.current;
    if (!sessionId || !connection) return;
    void connection
      .stop(sessionId)
      .catch((error: Error) => dispatch({ type: "error", message: error.message }));
  }, [state.sessionId]);

  const respondToApproval = useCallback(
    (id: string, approved: boolean) => {
      const sessionId = state.sessionId;
      const connection = connectionRef.current;
      const approval = state.approvals[id];
      if (!sessionId || !connection || !approval) return;
      setSendScrollNonce((n) => n + 1);
      dispatch({ type: "approval.resolved", approved, id });
      void connection
        .respondToApproval(sessionId, approval.payload, approved)
        .catch((error: Error) => dispatch({ type: "error", message: error.message }));
    },
    [state.approvals, state.sessionId],
  );

  const sessionPanel = (
    <ChatSessionList
      activeSessionId={activeSessionId}
      profile={profile}
      onPicked={closeMobilePanel}
      onNewChat={startNewGuiChat}
    />
  );

  const mobileSessionPortal =
    narrow &&
    portalRoot &&
    createPortal(
      <>
        {mobilePanelOpen && (
          <Button
            ghost
            aria-label="Close sessions"
            onClick={closeMobilePanel}
            className="fixed inset-0 z-[55] block bg-black/60 p-0"
          />
        )}

        <div
          id="gui-chat-session-panel"
          role="complementary"
          aria-label={t.sessions.title}
          className={cn(
            "font-mondwest fixed top-0 right-0 z-[60] flex h-dvh max-h-dvh w-72 min-w-0 flex-col antialiased",
            "border-l border-current/20 text-midground",
            "bg-background-base/95",
            "transition-transform duration-200 ease-out",
            "[background:var(--component-sidebar-background)]",
            "[clip-path:var(--component-sidebar-clip-path)]",
            "[border-image:var(--component-sidebar-border-image)]",
            mobilePanelOpen
              ? "translate-x-0"
              : "pointer-events-none translate-x-full",
          )}
        >
          <div className="flex h-14 shrink-0 items-center justify-between gap-2 border-b border-current/20 px-5">
            <div className="text-display text-sm font-bold tracking-wider text-midground">
              {t.sessions.title}
            </div>
            <Button
              ghost
              size="icon"
              onClick={closeMobilePanel}
              aria-label="Close sessions"
              className="text-text-secondary hover:text-midground"
            >
              <X />
            </Button>
          </div>

          <div className="min-h-0 flex-1 overflow-hidden px-1 py-2">
            {sessionPanel}
          </div>
        </div>
      </>,
      portalRoot,
    );

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-2">
      {mobileSessionPortal}
      <div className="flex min-h-0 flex-1 flex-col gap-2 lg:flex-row lg:gap-3">
        <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden border border-current/15 bg-background-base">
          <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-current/15 px-3 py-2 text-xs text-text-secondary sm:px-5">
            <Badge tone={statusTone}>{mockMode ? "mock" : state.connection}</Badge>
            {state.model ? <span className="truncate">Model: {state.model}</span> : null}
            {state.storedSessionId ? <span className="truncate">Session: {state.storedSessionId}</span> : null}
            <span className="ml-auto text-text-tertiary">
              {mockMode ? "mock structured events" : "/api/ws structured beta"}
            </span>
            <Button ghost size="sm" className="h-7 px-2 text-xs" onClick={connect}>
              <RefreshCw className="h-3.5 w-3.5" />
              {mockMode ? "Replay" : t.common.retry}
            </Button>
          </div>

          {state.error ? (
            <div className="flex shrink-0 items-start gap-2 border-b border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive sm:px-5">
              <AlertCircle className="mt-0.5 h-4 w-4" />
              <span className="min-w-0 flex-1 whitespace-pre-wrap">{state.error}</span>
            </div>
          ) : null}

          <MessageList
            disabled={disabled}
            forceBottomKey={forceBottomKey}
            onApprovalRespond={respondToApproval}
            state={state}
          />
          <Composer
            disabled={disabled}
            isGenerating={state.isGenerating}
            onSend={send}
            onStop={stop}
          />
        </div>

        {!narrow && (
          <div
            id="gui-chat-session-panel"
            role="complementary"
            aria-label={t.sessions.title}
            className="flex min-h-0 shrink-0 flex-col overflow-hidden lg:h-full lg:w-60"
          >
            {sessionPanel}
          </div>
        )}
      </div>
    </div>
  );
}

class AttachmentError extends Error {
  readonly attachmentId: string;

  constructor(attachmentId: string, message: string) {
    super(message);
    this.attachmentId = attachmentId;
    this.name = "AttachmentError";
  }
}

function toMessageAttachment(attachment: GuiComposerAttachment): MessageAttachmentState {
  return {
    id: attachment.id,
    kind: attachment.kind,
    mimeType: attachment.mimeType,
    name: attachment.name,
    pagesAttached: attachment.pagesAttached,
    previewUrl: attachment.previewUrl,
    refText: attachment.refText,
    sizeBytes: attachment.sizeBytes,
  };
}

function isBufferedStreamEvent(event: GatewayEvent): boolean {
  return (
    event.type === "message.delta" ||
    event.type === "thinking.delta" ||
    event.type === "reasoning.delta"
  );
}

function mergeBufferedStreamEvents(events: GatewayEvent[]): GatewayEvent[] {
  const merged: GatewayEvent[] = [];
  let pending: GatewayEvent | undefined;

  const flush = () => {
    if (pending) {
      merged.push(pending);
      pending = undefined;
    }
  };

  for (const event of events) {
    if (!isBufferedStreamEvent(event)) {
      flush();
      merged.push(event);
      continue;
    }

    if (!pending || pending.type !== event.type || pending.session_id !== event.session_id) {
      flush();
      pending = event;
      continue;
    }

    pending = mergeStreamEventPayload(pending, event);
  }

  flush();
  return merged;
}

function mergeStreamEventPayload(base: GatewayEvent, next: GatewayEvent): GatewayEvent {
  const basePayload = isRecord(base.payload) ? base.payload : undefined;
  const nextPayload = isRecord(next.payload) ? next.payload : undefined;
  if (!basePayload || !nextPayload) return next;

  const payload: Record<string, unknown> = { ...basePayload, ...nextPayload };
  for (const field of ["text", "rendered"] as const) {
    const baseText = basePayload[field];
    const nextText = nextPayload[field];
    if (typeof baseText === "string" || typeof nextText === "string") {
      payload[field] = `${typeof baseText === "string" ? baseText : ""}${
        typeof nextText === "string" ? nextText : ""
      }`;
    }
  }
  return { ...next, payload };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function appendFileReferences(text: string, fileRefs: string[]): string {
  if (fileRefs.length === 0) return text;
  return `${text.trim()}\n\n附件：\n${fileRefs.join("\n")}`.trim();
}

function createClientId(prefix: string): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}
