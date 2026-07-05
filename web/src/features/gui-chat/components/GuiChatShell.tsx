import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { AlertCircle, RefreshCw, Terminal } from "lucide-react";
import { usePageHeader } from "@/contexts/usePageHeader";
import { useProfileScope } from "@/contexts/useProfileScope";
import { useI18n } from "@/i18n";
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
  const [searchParams] = useSearchParams();
  const resumeSessionId = searchParams.get("resume");
  const mockMode = searchParams.get("mock") === "1";
  const [state, dispatch] = useReducer(guiChatReducer, initialGuiChatState);
  const connectionRef = useRef<GuiChatConnection | null>(null);
  const scopeKey = `${profile ?? ""}\0${resumeSessionId ?? ""}\0${mockMode ? "mock" : "live"}`;

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

  useEffect(() => connect(), [connect, scopeKey]);

  useEffect(() => {
    setTitle("Chat GUI (beta)");
    setEnd(
      <Button
        ghost
        size="sm"
        onClick={() =>
          navigate(
            resumeSessionId ? `/chat?resume=${encodeURIComponent(resumeSessionId)}` : "/chat",
          )
        }
      >
        <Terminal className="h-4 w-4" />
        Terminal Chat
      </Button>,
    );
    return () => {
      setTitle(null);
      setEnd(null);
    };
  }, [navigate, resumeSessionId, setEnd, setTitle]);

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

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden border border-current/15 bg-background-base">
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
  );
}

function createClientId(prefix: string): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `${prefix}-${crypto.randomUUID()}`;
  }
  return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}
