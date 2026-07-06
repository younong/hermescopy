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
import { cn } from "@/lib/utils";
import { connectGuiChat, type GuiChatConnection } from "../api";
import { connectMockGuiChat } from "../mock";
import { guiChatReducer } from "../reducer";
import { initialGuiChatState } from "../types";
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
  const [newChatNonce, setNewChatNonce] = useState(0);
  const [mobilePanelOpenRaw, setMobilePanelOpenRaw] = useState(false);
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

  const closeMobilePanel = useCallback(() => setMobilePanelOpenRaw(false), []);

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
    dispatch({ type: "reset" });
    const connection = mockMode
      ? connectMockGuiChat()
      : connectGuiChat({ profile, resumeSessionId });
    connectionRef.current = connection;
    const offState = connection.client.onState((next) => {
      dispatch({ type: "connection", state: next });
    });
    const offEvents = connection.client.onEvent((event) => {
      dispatch({ type: "event", event });
    });
    void connection
      .createOrResume()
      .then((response) => dispatch({ type: "session.created", response }))
      .catch((error: Error) => dispatch({ type: "error", message: error.message }));
    return () => {
      offState();
      offEvents();
      connection.close();
      if (connectionRef.current === connection) {
        connectionRef.current = null;
      }
    };
  }, [mockMode, profile, resumeSessionId]);

  useEffect(() => connect(), [connect, newChatNonce]);

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
    (text: string) => {
      const sessionId = state.sessionId;
      const connection = connectionRef.current;
      if (!sessionId || !connection) return;
      dispatch({ type: "user.sent", id: createClientId("user"), text });
      void connection
        .send(sessionId, text)
        .catch((error: Error) => dispatch({ type: "error", message: error.message }));
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

function createClientId(prefix: string): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}
