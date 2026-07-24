import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { Button } from "@nous-research/ui/ui/components/button";
import {
  AlertCircle,
  CircleHelp,
  FolderOpen,
  LogOut,
  Menu,
  MessageSquarePlus,
  RefreshCw,
  Search,
  QrCode,
  Settings2,
  Sparkles,
  X,
} from "lucide-react";
import { ChatSessionList } from "@/components/ChatSessionList";
import { ConnectWeChatModal } from "@/features/ilink/ConnectWeChatModal";
import { useProfileScope } from "@/contexts/useProfileScope";
import { GuiChatFilesPane } from "@/features/files/components/GuiChatFilesPane";
import { useI18n } from "@/i18n";
import { api } from "@/lib/api";
import { JsonRpcGatewayError, type GatewayEvent } from "@/lib/gatewayClient";
import { emitChatDiagnostic } from "@/lib/chatDiagnostics";
import { dashboardAuthTransition } from "@/lib/dashboardAuthTransition";
import { useDashboardAuthIdentity } from "@/lib/useDashboardAuthIdentity";
import { cn } from "@/lib/utils";
import { connectGuiChat, type GuiChatConnection } from "../api";
import { buildSessionFileDownloadUrl } from "../files";
import { createGatewayEventFrameQueue } from "../gatewayEventFrameQueue";
import { startGuiChatLatencyTrace, type GuiChatLatencyTrace } from "../latencyTrace";
import { connectMockGuiChat } from "../mock";
import { guiChatReducer } from "../reducer";
import { GuiChatReconnectLifecycle } from "../reconnectLifecycle";
import { GuiChatSessionSwitchCoordinator } from "../sessionSwitch";
import {
  initialGuiChatState,
  type GuiComposerAttachment,
  type MessageAttachmentState,
} from "../types";
import { Composer } from "./Composer";
import { MessageList } from "./MessageList";

export function GuiChatShell() {
  const { t } = useI18n();
  const location = useLocation();
  const navigate = useNavigate();
  const { profile } = useProfileScope();
  const [searchParams, setSearchParams] = useSearchParams();
  const resumeSessionId = searchParams.get("resume");
  const mockMode = searchParams.get("mock") === "1";
  const filesOpen = location.pathname.replace(/\/$/, "") === "/chat-gui/files";
  const [state, dispatch] = useReducer(guiChatReducer, initialGuiChatState);
  const connectionRef = useRef<GuiChatConnection | null>(null);
  const historyAbortRef = useRef<AbortController | null>(null);
  const reconnectLifecycleRef = useRef<GuiChatReconnectLifecycle | null>(null);
  const eventFrameQueue = useMemo(
    () => createGatewayEventFrameQueue(
      (event) => dispatch({ type: "event", event }),
      undefined,
      undefined,
      {
        onDiagnostic: (summary) => {
          connectionRef.current?.reportFrameQueueDiagnostic(summary);
        },
      },
    ),
    [],
  );
  const latencyTraceRef = useRef<GuiChatLatencyTrace | null>(null);
  const switchCoordinatorRef = useRef<GuiChatSessionSwitchCoordinator | null>(null);
  const canonicalRouteRef = useRef<string | null>(null);
  const skipClearedRouteRef = useRef(false);
  const switchTraceByGenerationRef = useRef(new Map<number, GuiChatLatencyTrace>());
  const [resumeNotice, setResumeNotice] = useState<string | null>(null);
  const [sendScrollNonce, setSendScrollNonce] = useState(0);
  const [mobilePanelOpenRaw, setMobilePanelOpenRaw] = useState(false);
  const [sessionQuery, setSessionQuery] = useState("");
  const [activeSessionTitle, setActiveSessionTitle] = useState<string | null>(null);
  const [connectWeChatOpen, setConnectWeChatOpen] = useState(false);
  const { authMe, authRequired, ownerKey, ready: authIdentityReady } = useDashboardAuthIdentity();
  const weChatStatus = authMe?.feature_status?.weixin_ilink_connect;
  const weChatReady = Boolean(authMe?.features?.weixin_ilink_connect);
  const canConnectWeChat = Boolean(
    authRequired && authIdentityReady && (weChatStatus?.enabled ?? weChatReady),
  );
  const weChatUnavailableMessage = weChatReady
    ? undefined
    : weChatStatus?.message ?? "WeChat connection is not available on this server yet.";
  const stateRef = useRef(state);
  const filesOpenRef = useRef(filesOpen);
  const navigateRef = useRef(navigate);
  const resumeSessionIdRef = useRef(resumeSessionId);
  const setSearchParamsRef = useRef(setSearchParams);
  stateRef.current = state;
  filesOpenRef.current = filesOpen;
  navigateRef.current = navigate;
  resumeSessionIdRef.current = resumeSessionId;
  setSearchParamsRef.current = setSearchParams;
  const updateSearchParams = useCallback(
    (
      nextInit: Parameters<typeof setSearchParams>[0],
      navigateOptions?: Parameters<typeof setSearchParams>[1],
    ) => setSearchParamsRef.current(nextInit, navigateOptions),
    [],
  );
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
  const forceBottomKey = `${activeSessionId ?? "new"}:${sendScrollNonce}`;
  const closeMobilePanel = useCallback(() => setMobilePanelOpenRaw(false), []);
  const handleActiveSessionChange = useCallback(
    (session: { id: string; label: string } | null) =>
      setActiveSessionTitle(session?.label ?? null),
    [],
  );
  const startSessionSwitchTrace = useCallback((_sessionId: string) => {
    reconnectLifecycleRef.current?.cancelRecovery();
    latencyTraceRef.current?.mark("switch.superseded", "cancelled");
    switchTraceByGenerationRef.current.clear();
    latencyTraceRef.current = startGuiChatLatencyTrace("session_list.click");
  }, []);

  useEffect(() => dashboardAuthTransition.register(() => {
    historyAbortRef.current?.abort();
    historyAbortRef.current = null;
    reconnectLifecycleRef.current?.dispose();
    reconnectLifecycleRef.current = null;
    switchCoordinatorRef.current?.dispose();
    switchCoordinatorRef.current = null;
    connectionRef.current = null;
    eventFrameQueue.reset();
    dispatch({ type: "reset" });
  }), [eventFrameQueue]);

  const dispatchGatewayEvent = useCallback((event: GatewayEvent) => {
    eventFrameQueue.enqueue(event);
  }, [eventFrameQueue]);

  const startNewGuiChat = useCallback(() => {
    historyAbortRef.current?.abort();
    reconnectLifecycleRef.current?.cancelRecovery();
    setResumeNotice(null);
    skipClearedRouteRef.current = true;
    if (filesOpenRef.current) {
      navigateRef.current("/chat-gui", { replace: true });
    } else {
      updateSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.delete("resume");
          return next;
        },
        { replace: true },
      );
    }
    switchCoordinatorRef.current?.start(null);
  }, [updateSearchParams]);

  const switchScope = useMemo(() => {
    const connection = mockMode
      ? connectMockGuiChat()
      : connectGuiChat({ ownerKey, profile });
    connectionRef.current = connection;
    let coordinator: GuiChatSessionSwitchCoordinator;
    coordinator = new GuiChatSessionSwitchCoordinator(connection, {
      onCommit: (_connection, response, requestedSessionId, generation) => {
        historyAbortRef.current?.abort();
        const trace = switchTraceByGenerationRef.current.get(generation);
        switchTraceByGenerationRef.current.delete(generation);
        dispatch({ type: "session.created", response });
        emitChatDiagnostic({
          event: "initial_page",
          loadedCount: response.messages?.length ?? 0,
          outcome: "ok",
          renderedCount: response.messages?.length ?? 0,
          surface: "gui_history",
        });
        reconnectLifecycleRef.current?.onSwitchSettled(generation, true);

        if (requestedSessionId && "resumed" in response) {
          const canonicalSessionId = response.resumed ?? response.session_key ?? requestedSessionId;
          if (canonicalSessionId !== requestedSessionId) {
            trace?.mark("session.canonicalized", "ok");
            canonicalRouteRef.current = canonicalSessionId;
            updateSearchParams(
              (prev) => {
                if (prev.get("resume") !== requestedSessionId) return prev;
                const next = new URLSearchParams(prev);
                next.set("resume", canonicalSessionId);
                return next;
              },
              { replace: true },
            );
          }
        }

        requestAnimationFrame(() => {
          if (!switchCoordinatorRef.current?.isGenerationCurrent(generation)) return;
          trace?.mark("transcript.paint", "ok");
          if (latencyTraceRef.current === trace) latencyTraceRef.current = null;
        });
      },
      onError: (error, requestedSessionId, generation, committedSessionId) => {
        const trace = switchTraceByGenerationRef.current.get(generation);
        switchTraceByGenerationRef.current.delete(generation);
        reconnectLifecycleRef.current?.onSwitchSettled(generation, false);
        trace?.mark(requestedSessionId ? "session.attach.end" : "session.create.end", "error");
        if (latencyTraceRef.current === trace) latencyTraceRef.current = null;

        if (requestedSessionId && committedSessionId && requestedSessionId !== committedSessionId) {
          canonicalRouteRef.current = committedSessionId;
          updateSearchParams(
            (prev) => {
              if (prev.get("resume") !== requestedSessionId) return prev;
              const next = new URLSearchParams(prev);
              next.set("resume", committedSessionId);
              return next;
            },
            { replace: true },
          );
        }

        if (error instanceof JsonRpcGatewayError && error.code === 4007) {
          if (committedSessionId) {
            setResumeNotice("This session is no longer available. The current chat was kept open.");
          } else {
            startNewGuiChat();
            setResumeNotice("This session is no longer available. Started a new chat instead.");
          }
          return;
        }
        dispatch({
          type: "error",
          message: error instanceof Error ? error.message : String(error),
        });
      },
      onEvent: (event) => dispatchGatewayEvent(event),
      onEventObserved: (event, generation) => {
        if (event.type === "gateway.ready") {
          switchTraceByGenerationRef.current.get(generation)?.mark("gateway.ready");
        }
      },
      onState: (next) => {
        dispatch({ type: "connection", state: next });
        reconnectLifecycleRef.current?.onConnectionState(next);
      },
    });
    const reconnectLifecycle = mockMode
      ? null
      : new GuiChatReconnectLifecycle({
          close: () => connection.close(),
          ping: () => connection.ping(),
          reconnect: () =>
            coordinator.start(
              coordinator.committedSessionId ??
                stateRef.current.storedSessionId ??
                resumeSessionIdRef.current,
            ),
        });
    reconnectLifecycleRef.current = reconnectLifecycle;
    return { coordinator, reconnectLifecycle };
  }, [dispatchGatewayEvent, mockMode, ownerKey, profile, startNewGuiChat, updateSearchParams]);
  const switchCoordinator = switchScope.coordinator;
  switchCoordinatorRef.current = switchCoordinator;

  const connectRoute = useCallback(() => {
    reconnectLifecycleRef.current?.cancelRecovery();
    setResumeNotice(null);
    const trace = latencyTraceRef.current;
    trace?.mark("connection.start");
    const nextGeneration = switchCoordinator.currentGeneration + 1;
    if (trace) switchTraceByGenerationRef.current.set(nextGeneration, trace);
    switchCoordinator.start(
      resumeSessionId,
      trace
        ? {
            onStage: (stage) => trace.mark(stage),
            onSwitchStage: (stage) => trace.mark(stage),
            traceId: trace.id,
          }
        : undefined,
    );
  }, [resumeSessionId, switchCoordinator]);

  const retryConnection = useCallback(() => {
    setResumeNotice(null);
    if (mockMode) {
      connectRoute();
      return;
    }
    reconnectLifecycleRef.current?.retryNow();
  }, [connectRoute, mockMode]);

  useEffect(() => {
    if (!authIdentityReady) return;
    if (canonicalRouteRef.current !== null && canonicalRouteRef.current === resumeSessionId) {
      canonicalRouteRef.current = null;
      return;
    }
    if (skipClearedRouteRef.current && resumeSessionId === null) {
      skipClearedRouteRef.current = false;
      return;
    }
    connectRoute();
  }, [authIdentityReady, connectRoute, resumeSessionId]);

  useEffect(
    () => () => {
      historyAbortRef.current?.abort();
      eventFrameQueue.reset();
      switchScope.reconnectLifecycle?.dispose();
      switchCoordinator.dispose();
      switchTraceByGenerationRef.current.clear();
      if (reconnectLifecycleRef.current === switchScope.reconnectLifecycle) {
        reconnectLifecycleRef.current = null;
      }
      if (switchCoordinatorRef.current === switchCoordinator) {
        switchCoordinatorRef.current = null;
      }
    },
    [eventFrameQueue, switchCoordinator, switchScope.reconnectLifecycle],
  );

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

  const disabled = state.connection !== "open" || !state.sessionId;
  const hasPendingClarification = state.clarificationOrder.some((id) =>
    ["pending", "submitting"].includes(state.clarifications[id]?.status ?? ""),
  );
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
            messageAttachments.push(toMessageAttachment(sentAttachment, state.cwd));
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
                height: validImageDimensions(result.width, result.height)?.height,
                stagedSessionId: sessionId,
                status: "uploaded",
                width: validImageDimensions(result.width, result.height)?.width,
              };
              updateAttachment(attachment.id, sentAttachment);
            } else if (attachment.kind === "pdf") {
              const result = await connection.attachPdf(sessionId, attachment.file);
              if (!result.attached) {
                throw new Error(result.message || `Could not attach ${attachment.name}`);
              }
              sentAttachment = {
                ...attachment,
                attachedPath: result.path,
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
          messageAttachments.push(toMessageAttachment(sentAttachment, state.cwd));
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

  const loadEarlier = useCallback(async () => {
    const connection = connectionRef.current;
    const sessionId = state.sessionId;
    const cursor = state.historyCursor;
    if (!connection || !sessionId || !cursor || state.historyLoading) return;
    historyAbortRef.current?.abort();
    const controller = new AbortController();
    historyAbortRef.current = controller;
    dispatch({ type: "history.prepend.started" });
    const startedAt = performance.now();
    try {
      const response = await connection.loadEarlier(sessionId, cursor, controller.signal);
      if (controller.signal.aborted) return;
      dispatch({ type: "history.prepend.succeeded", response });
      emitChatDiagnostic({
        durationMs: Math.round(performance.now() - startedAt),
        event: "page_loaded",
        loadedCount: response.messages?.length ?? 0,
        outcome: "ok",
        surface: "gui_history",
      });
    } catch (error) {
      if (controller.signal.aborted) return;
      emitChatDiagnostic({
        durationMs: Math.round(performance.now() - startedAt),
        event: "page_loaded",
        outcome: "error",
        surface: "gui_history",
      });
      if (error instanceof JsonRpcGatewayError && error.code === -32601) {
        dispatch({ type: "history.prepend.failed", message: "Earlier history is unavailable on this server version." });
        return;
      }
      dispatch({
        type: "history.prepend.failed",
        message: error instanceof Error ? error.message : String(error),
      });
    } finally {
      if (historyAbortRef.current === controller) historyAbortRef.current = null;
    }
  }, [state.historyCursor, state.historyLoading, state.sessionId]);

  const respondToClarify = useCallback(
    (id: string, answer: string) => {
      const sessionId = state.sessionId;
      const connection = connectionRef.current;
      const clarification = state.clarifications[id];
      if (!sessionId || !connection || clarification?.status !== "pending") return;
      setSendScrollNonce((n) => n + 1);
      dispatch({ type: "clarify.submitting", id });
      void connection
        .respondToClarify(sessionId, id, answer)
        .catch((error: Error) => dispatch({ type: "error", message: error.message }));
    },
    [state.clarifications, state.sessionId],
  );

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
      onActiveSessionChange={handleActiveSessionChange}
      onNewChat={startNewGuiChat}
      onPicked={closeMobilePanel}
      onSessionPick={startSessionSwitchTrace}
      profile={profile}
      query={sessionQuery}
      sessionPath="/chat-gui"
      variant="compact"
    />
  );
  const conversationTitle = activeSessionTitle ?? (activeSessionId ? "Conversation" : "New chat");
  const accountLabel = authMe?.display_name || authMe?.email || "Hermes workspace";
  const handleLogout = () => {
    dashboardAuthTransition.reset();
    void api.logout();
  };
  const sidebar = (
    <>
      <div className="px-3 pb-2 pt-3">
        <div className="gui-chat-search">
          <Search aria-hidden className="h-3.5 w-3.5 shrink-0" />
          <input
            aria-label="Search conversations"
            onChange={(event) => setSessionQuery(event.target.value)}
            placeholder="Search"
            value={sessionQuery}
          />
        </div>
      </div>
      <nav aria-label="Chat navigation" className="space-y-[3px] px-3">
        <button
          aria-current={!filesOpen && !resumeSessionId ? "page" : undefined}
          className="gui-chat-nav-item"
          onClick={startNewGuiChat}
          type="button"
        >
          <MessageSquarePlus />
          <span>New chat</span>
        </button>
        <button
          aria-current={filesOpen ? "page" : undefined}
          className="gui-chat-nav-item"
          onClick={() => {
            closeMobilePanel();
            navigate("/chat-gui/files");
          }}
          type="button"
        >
          <FolderOpen />
          <span>Files</span>
        </button>
        <button className="gui-chat-nav-item" onClick={() => navigate("/skills")} type="button">
          <Sparkles />
          <span>Skills</span>
        </button>
      </nav>
      <div className="mt-4 flex min-h-0 flex-1 flex-col px-3">
        <div className="gui-chat-section-heading">
          <span>Recent chats</span>
          <button aria-label={t.common.refresh} className="gui-chat-icon-button" onClick={retryConnection} type="button">
            <RefreshCw className={cn("h-3.5 w-3.5", state.connection === "connecting" && "animate-spin")} />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-hidden">{sessionPanel}</div>
      </div>
      <div className="mx-3 border-t border-black/[0.06] py-1.5">
        <div className="gui-chat-account-row">
          <button className="gui-chat-account" onClick={() => navigate("/system")} type="button">
            <span className="gui-chat-avatar">{accountLabel.trim().charAt(0).toUpperCase() || "H"}</span>
            <span className="min-w-0 flex-1 truncate text-left">{accountLabel}</span>
            <Settings2 className="h-3.5 w-3.5 text-[#8a8e95]" />
          </button>
          {authRequired && authMe ? (
            <button aria-label="Log out" className="gui-chat-logout" onClick={handleLogout} title="Log out" type="button">
              <LogOut />
            </button>
          ) : null}
        </div>
        <button className="gui-chat-nav-item mt-0.5" onClick={() => navigate("/docs")} type="button">
          <CircleHelp />
          <span>Help</span>
        </button>
      </div>
    </>
  );

  const mobileSessionPortal =
    narrow &&
    portalRoot &&
    createPortal(
      <>
        {mobilePanelOpen && (
          <Button
            ghost
            aria-label="Dismiss session drawer"
            onClick={closeMobilePanel}
            className="fixed inset-0 z-[55] block bg-black/60 p-0"
          />
        )}

        <aside
          data-gui-chat
          id="gui-chat-session-panel"
          aria-label="Chat workspace"
          className={cn(
            "gui-chat-mobile-sidebar fixed left-0 top-0 z-[60] flex h-dvh max-h-dvh min-w-0 flex-col shadow-2xl",
            "transition-transform duration-200 ease-out",
            mobilePanelOpen
              ? "translate-x-0"
              : "pointer-events-none -translate-x-full",
          )}
        >
          <div className="flex h-11 shrink-0 items-center justify-between px-3">
            <span className="text-sm font-semibold">Hermes</span>
            <button aria-label="Close sessions" className="gui-chat-icon-button" onClick={closeMobilePanel} type="button">
              <X className="h-4 w-4" />
            </button>
          </div>
          {sidebar}
        </aside>
      </>,
      portalRoot,
    );

  return (
    <div data-gui-chat className="relative z-1 flex h-dvh min-h-0 w-full overflow-hidden bg-white text-[#202124]">
      {mobileSessionPortal}
      {connectWeChatOpen ? (
        <ConnectWeChatModal
          onClose={() => setConnectWeChatOpen(false)}
          unavailableMessage={weChatUnavailableMessage}
        />
      ) : null}
      {!narrow ? (
        <aside aria-label="Chat workspace" className="gui-chat-sidebar">
          {sidebar}
        </aside>
      ) : null}

      <main className="flex min-h-0 min-w-0 flex-1 flex-col bg-white">
        <header className="relative flex h-12 shrink-0 items-center border-b border-[#ebecef] px-3 sm:px-4">
          {narrow ? (
            <button
              aria-controls="gui-chat-session-panel"
              aria-expanded={mobilePanelOpen}
              aria-label="Open sessions"
              className="gui-chat-icon-button"
              onClick={() => setMobilePanelOpenRaw(true)}
              type="button"
            >
              <Menu className="h-4 w-4" />
            </button>
          ) : <div className="w-8" />}
          <div className="pointer-events-none absolute inset-x-20 top-1/2 min-w-0 -translate-y-1/2 text-center">
            <h1 className="truncate text-[0.8125rem] font-medium text-[#25282d]">
              {filesOpen ? "Files" : conversationTitle}
            </h1>
            <p className="truncate text-[0.625rem] text-[#969aa1]">
              {filesOpen ? "Workspace" : `${state.model ?? "Hermes"} · ${mockMode ? "mock" : state.connection}`}
            </p>
          </div>
          {!filesOpen ? (
            <div className="ml-auto flex items-center gap-1">
              {canConnectWeChat ? (
                <button
                  aria-label="Connect WeChat"
                  className="gui-chat-icon-button"
                  onClick={() => setConnectWeChatOpen(true)}
                  title="Connect WeChat"
                  type="button"
                >
                  <QrCode className="h-3.5 w-3.5" />
                </button>
              ) : null}
              <button aria-label={mockMode ? "Replay" : t.common.retry} className="gui-chat-icon-button" onClick={retryConnection} type="button">
                <RefreshCw className={cn("h-3.5 w-3.5", state.connection === "connecting" && "animate-spin")} />
              </button>
            </div>
          ) : null}
        </header>

        {filesOpen ? (
          <GuiChatFilesPane />
        ) : (
          <>
            {resumeNotice ? (
              <div className="gui-chat-notice">
                <AlertCircle />
                <span>{resumeNotice}</span>
              </div>
            ) : null}
            {state.error ? (
              <div className="gui-chat-notice gui-chat-notice-error">
                <AlertCircle />
                <span>{state.error}</span>
              </div>
            ) : null}

            <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden">
              <MessageList
                disabled={disabled}
                forceBottomKey={forceBottomKey}
                onApprovalRespond={respondToApproval}
                onClarifyRespond={respondToClarify}
                onLoadEarlier={loadEarlier}
                state={state}
              />
              <Composer
                allowSendWhileGenerating={hasPendingClarification}
                disabled={disabled}
                isGenerating={state.isGenerating}
                onSend={send}
                onStop={stop}
              />
            </div>
          </>
        )}
      </main>
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

function toMessageAttachment(
  attachment: GuiComposerAttachment,
  cwd?: string,
): MessageAttachmentState {
  return {
    downloadUrl: attachment.attachedPath
      ? buildSessionFileDownloadUrl(attachment.attachedPath, cwd, attachment.name)
      : undefined,
    id: attachment.id,
    kind: attachment.kind,
    mimeType: attachment.mimeType,
    name: attachment.name,
    pagesAttached: attachment.pagesAttached,
    previewUrl: attachment.previewUrl,
    refText: attachment.refText,
    sizeBytes: attachment.sizeBytes,
    sourcePath: attachment.attachedPath,
    height: attachment.height,
    width: attachment.width,
  };
}

function validImageDimensions(
  width: unknown,
  height: unknown,
): { height: number; width: number } | undefined {
  if (
    typeof width !== "number" || !Number.isFinite(width) || width <= 0 ||
    typeof height !== "number" || !Number.isFinite(height) || height <= 0
  ) {
    return undefined;
  }
  return { height, width };
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
