import { useCallback, useEffect, useMemo, useReducer, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { Button } from "@nous-research/ui/ui/components/button";
import {
  AlertCircle,
  CalendarClock,
  CircleHelp,
  FolderOpen,
  LogOut,
  Menu,
  MessageSquarePlus,
  Bot,
  PieChart,
  RefreshCw,
  Search,
  QrCode,
  Radio,
  Settings2,
  UsersRound,
  SlidersHorizontal,
  Sparkles,
  X,
} from "lucide-react";
import { ChatSessionList } from "@/components/ChatSessionList";
import { CreateGroupDialog } from "@/features/collaboration/components/CreateGroupDialog";
import { GroupChatView } from "@/features/collaboration/components/GroupChatView";
import { GroupsSidebar } from "@/features/collaboration/components/GroupsSidebar";
import { parseChatRoute } from "@/features/collaboration/routing";
import type { CollaborationGroup } from "@/features/collaboration/types";
import { ConnectWeChatModal } from "@/features/ilink/ConnectWeChatModal";
import { PageHeaderContext } from "@/contexts/page-header-context";
import { GuiChatFilesPane } from "@/features/files/components/GuiChatFilesPane";
import { useI18n } from "@/i18n";
import ChannelsPage from "@/pages/ChannelsPage";
import SessionsPage from "@/pages/SessionsPage";
import { api, type FeishuEmployee } from "@/lib/api";
import { JsonRpcGatewayError, type GatewayEvent } from "@/lib/gatewayClient";
import { emitChatDiagnostic } from "@/lib/chatDiagnostics";
import { dashboardAuthTransition } from "@/lib/dashboardAuthTransition";
import { useDashboardAuthIdentity } from "@/lib/useDashboardAuthIdentity";
import { cn } from "@/lib/utils";
import { connectGuiChat, type GuiChatConnection } from "../api";
import { buildSessionFileDownloadUrl, readSessionFile } from "../files";
import { createGatewayEventFrameQueue } from "../gatewayEventFrameQueue";
import {
  navigationStartedAt,
  startGuiChatLatencyTrace,
  type GuiChatLatencyTrace,
} from "../latencyTrace";
import { connectMockGuiChat } from "../mock";
import { guiChatReducer } from "../reducer";
import { WebSocketReconnectLifecycle } from "../reconnectLifecycle";
import { GuiChatSessionSwitchCoordinator } from "../sessionSwitch";
import {
  initialGuiChatState,
  type GuiComposerAttachment,
  type MessageAttachmentState,
} from "../types";
import { Composer } from "./Composer";
import { ComposerModelPicker } from "./ComposerModelPicker";
import { GuiChatModelsPane } from "./GuiChatModelsPane";
import { GuiChatScheduledTasksPane } from "./GuiChatScheduledTasksPane";
import { GuiChatSkillsPane } from "./GuiChatSkillsPane";
import { MessageList } from "./MessageList";

const EMBEDDED_PAGE_HEADER = {
  setAfterTitle: (_node: ReactNode) => undefined,
  setEnd: (_node: ReactNode) => undefined,
  setTitle: (_title: string | null) => undefined,
};

export function GuiChatShell() {
  const { t } = useI18n();
  const location = useLocation();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const routeTarget = parseChatRoute(searchParams);
  const groupId = routeTarget.kind === "group" ? routeTarget.id : null;
  const resumeSessionId = routeTarget.kind === "direct" ? routeTarget.id : null;
  const mockMode = searchParams.get("mock") === "1";
  const workspacePath = location.pathname.replace(/\/$/, "");
  const statisticsOpen = workspacePath === "/chat/statistics";
  const filesOpen = workspacePath === "/chat/files";
  const skillsOpen = workspacePath === "/chat/skills";
  const scheduledTasksOpen = workspacePath === "/chat/scheduled-tasks";
  const robotsOpen = workspacePath === "/chat/robots";
  const modelsOpen = workspacePath === "/chat/models";
  const workspacePaneOpen = statisticsOpen || filesOpen || skillsOpen || scheduledTasksOpen || robotsOpen || modelsOpen;
  const [state, dispatch] = useReducer(guiChatReducer, initialGuiChatState);
  const connectionRef = useRef<GuiChatConnection | null>(null);
  const historyAbortRef = useRef<AbortController | null>(null);
  const reconnectLifecycleRef = useRef<WebSocketReconnectLifecycle | null>(null);
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
  const [attachmentsToQueue, setAttachmentsToQueue] = useState<Array<{
    file: File;
    requestId: number;
  }>>([]);
  const attachmentRequestIdRef = useRef(0);
  const createGroupAttemptRef = useRef<{
    accountIds: string[];
    key: string;
    name: string;
  } | null>(null);
  const [mobilePanelOpenRaw, setMobilePanelOpenRaw] = useState(false);
  const [sessionQuery, setSessionQuery] = useState("");
  const [sessionListRefreshNonce, setSessionListRefreshNonce] = useState(0);
  const [activeSessionTitle, setActiveSessionTitle] = useState<string | null>(null);
  const [connectWeChatOpen, setConnectWeChatOpen] = useState(false);
  const [groups, setGroups] = useState<CollaborationGroup[]>([]);
  const [groupsLoading, setGroupsLoading] = useState(false);
  const [groupsRefreshNonce, setGroupsRefreshNonce] = useState(0);
  const [createGroupOpen, setCreateGroupOpen] = useState(false);
  const [employees, setEmployees] = useState<FeishuEmployee[]>([]);
  const [employeeLoadStatus, setEmployeeLoadStatus] = useState<"loading" | "ready" | "error">("loading");
  const [employeeChatOpen, setEmployeeChatOpen] = useState(false);
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
  const workspacePaneOpenRef = useRef(workspacePaneOpen);
  const navigateRef = useRef(navigate);
  const resumeSessionIdRef = useRef(resumeSessionId);
  const setSearchParamsRef = useRef(setSearchParams);
  stateRef.current = state;
  workspacePaneOpenRef.current = workspacePaneOpen;
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
  const activeSessionId = state.historySessionId ?? resumeSessionId;
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
    setAttachmentsToQueue([]);
    reconnectLifecycleRef.current?.cancelRecovery();
    setResumeNotice(null);
    skipClearedRouteRef.current = true;
    if (workspacePaneOpenRef.current) {
      navigateRef.current("/chat", { replace: true });
    } else {
      updateSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.delete("resume");
          next.delete("group");
          return next;
        },
        { replace: true },
      );
    }
    const coordinator = switchCoordinatorRef.current;
    if (coordinator) {
      const generation = coordinator.start(null);
      dispatch({ type: "session.selected", generation, sessionId: null });
    }
  }, [updateSearchParams]);

  const startEmployeeChat = (accountId: string) => {
    const employee = employees.find((item) => item.account_id === accountId);
    if (
      !employee
      || employee.lifecycle_status !== "active"
      || !employee.profile
      || !employee.collaboration_policy.may_participate
    ) {
      dispatch({ type: "error", message: "This employee is unavailable for direct chat." });
      return;
    }
    setEmployeeChatOpen(false);
    historyAbortRef.current?.abort();
    setAttachmentsToQueue([]);
    reconnectLifecycleRef.current?.cancelRecovery();
    setResumeNotice(null);
    skipClearedRouteRef.current = true;
    updateSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.delete("resume");
      next.delete("group");
      return next;
    }, { replace: true });
    const coordinator = switchCoordinatorRef.current;
    if (!coordinator) return;
    const generation = coordinator.start(
      null,
      undefined,
      { employeeAccountId: employee.account_id },
    );
    dispatch({ type: "session.selected", generation, sessionId: null });
  };

  const switchScope = useMemo(() => {
    const connection = mockMode
      ? connectMockGuiChat()
      : connectGuiChat({ ownerKey });
    connectionRef.current = connection;
    let coordinator: GuiChatSessionSwitchCoordinator;
    coordinator = new GuiChatSessionSwitchCoordinator(connection, {
      onCommit: (_connection, response, _requestedSessionId, generation) => {
        const trace = switchTraceByGenerationRef.current.get(generation);
        switchTraceByGenerationRef.current.delete(generation);
        dispatch({ type: "session.created", response });
        reconnectLifecycleRef.current?.onReconnectSettled(generation, true);

        requestAnimationFrame(() => {
          if (!switchCoordinatorRef.current?.isGenerationCurrent(generation)) return;
          trace?.mark("transcript.paint", "ok");
          if (latencyTraceRef.current === trace) latencyTraceRef.current = null;
        });
      },
      onError: (error, requestedSessionId, generation, committedSessionId) => {
        const trace = switchTraceByGenerationRef.current.get(generation);
        switchTraceByGenerationRef.current.delete(generation);
        reconnectLifecycleRef.current?.onReconnectSettled(generation, false);
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
      : new WebSocketReconnectLifecycle({
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
  }, [dispatchGatewayEvent, mockMode, ownerKey, startNewGuiChat, updateSearchParams]);
  const switchCoordinator = switchScope.coordinator;
  switchCoordinatorRef.current = switchCoordinator;

  const connectRoute = useCallback(() => {
    if (groupId) {
      historyAbortRef.current?.abort();
      setAttachmentsToQueue([]);
      switchCoordinator.cancel();
      void connectionRef.current?.attachOwner().catch((error: unknown) => {
        dispatch({ type: "error", message: error instanceof Error ? error.message : String(error) });
      });
      return;
    }
    reconnectLifecycleRef.current?.cancelRecovery();
    setResumeNotice(null);
    historyAbortRef.current?.abort();
    setAttachmentsToQueue([]);
    const existingTrace = latencyTraceRef.current;
    const navigationStart = navigationStartedAt();
    const trace = existingTrace ?? startGuiChatLatencyTrace(
      "connection.start",
      navigationStart === undefined ? undefined : { startedAt: navigationStart },
    );
    latencyTraceRef.current = trace;
    if (existingTrace) trace.mark("connection.start");
    const nextGeneration = switchCoordinator.currentGeneration + 1;
    if (trace) switchTraceByGenerationRef.current.set(nextGeneration, trace);
    dispatch({ type: "session.selected", generation: nextGeneration, sessionId: resumeSessionId });
    if (resumeSessionId && !mockMode) {
      const controller = new AbortController();
      historyAbortRef.current = controller;
      const requestedSessionId = resumeSessionId;
      const startedAt = performance.now();
      void api.getSessionMessages(
        requestedSessionId,
        { limit: 100, signal: controller.signal },
      ).then((response) => {
        if (controller.signal.aborted || !switchCoordinator.isGenerationCurrent(nextGeneration)) return;
        dispatch({
          type: "history.initial.succeeded",
          generation: nextGeneration,
          requestedSessionId,
          response,
        });
        emitChatDiagnostic({
          durationMs: Math.round(performance.now() - startedAt),
          event: "initial_page",
          loadedCount: response.messages.length,
          outcome: "ok",
          renderedCount: response.messages.length,
          surface: "gui_history",
        });
        if (response.session_id !== requestedSessionId) {
          trace?.mark("session.canonicalized", "ok");
          canonicalRouteRef.current = response.session_id;
          updateSearchParams((prev) => {
            if (prev.get("resume") !== requestedSessionId) return prev;
            const next = new URLSearchParams(prev);
            next.set("resume", response.session_id);
            return next;
          }, { replace: true });
        }
      }).catch((error: unknown) => {
        if (controller.signal.aborted || !switchCoordinator.isGenerationCurrent(nextGeneration)) return;
        dispatch({
          type: "history.initial.failed",
          generation: nextGeneration,
          message: error instanceof Error ? error.message : String(error),
          requestedSessionId,
        });
        emitChatDiagnostic({
          durationMs: Math.round(performance.now() - startedAt),
          event: "initial_page",
          outcome: "error",
          surface: "gui_history",
        });
      }).finally(() => {
        if (historyAbortRef.current === controller) historyAbortRef.current = null;
      });
    }
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
  }, [groupId, mockMode, resumeSessionId, switchCoordinator, updateSearchParams]);

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
  }, [authIdentityReady, connectRoute, groupId, resumeSessionId]);

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

  const refreshGroups = useCallback(() => setGroupsRefreshNonce((nonce) => nonce + 1), []);

  useEffect(() => {
    if (!authIdentityReady) return;
    const connection = connectionRef.current;
    if (!connection) return;
    const controller = new AbortController();
    setGroupsLoading(true);
    void connection.collaboration.listGroups(true, controller.signal).then((response) => {
      if (!controller.signal.aborted) setGroups(response.groups);
    }).catch(() => undefined).finally(() => {
      if (!controller.signal.aborted) setGroupsLoading(false);
    });
    setEmployeeLoadStatus("loading");
    void api.getFeishuEmployees().then((response) => {
      if (controller.signal.aborted) return;
      setEmployees(response.employees);
      setEmployeeLoadStatus("ready");
    }).catch(() => {
      if (controller.signal.aborted) return;
      setEmployees([]);
      setEmployeeLoadStatus("error");
    });
    return () => controller.abort();
  }, [authIdentityReady, groupsRefreshNonce, switchScope]);

  useEffect(() => {
    const connection = connectionRef.current;
    if (!connection) return;
    return connection.collaboration.onEvent((event) => {
      if (event.type === "collaboration.group.changed") refreshGroups();
    });
  }, [refreshGroups, switchScope]);

  const pickGroup = useCallback((nextGroupId: string) => {
    closeMobilePanel();
    setResumeNotice(null);
    updateSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.delete("resume");
      next.set("group", nextGroupId);
      return next;
    });
  }, [closeMobilePanel, updateSearchParams]);

  const createGroup = useCallback(async (name: string, accountIds: string[]) => {
    const connection = connectionRef.current;
    if (!connection) throw new Error("Gateway is not ready");
    const previousAttempt = createGroupAttemptRef.current;
    const sameAttempt = previousAttempt?.name === name
      && previousAttempt.accountIds.length === accountIds.length
      && previousAttempt.accountIds.every((accountId, index) => accountId === accountIds[index]);
    const attempt = previousAttempt && sameAttempt
      ? previousAttempt
      : { accountIds: [...accountIds], key: crypto.randomUUID(), name };
    createGroupAttemptRef.current = attempt;
    const snapshot = await connection.collaboration.createGroup(
      name,
      accountIds,
      attempt.key,
    );
    createGroupAttemptRef.current = null;
    setCreateGroupOpen(false);
    refreshGroups();
    pickGroup(snapshot.group.group_id);
  }, [pickGroup, refreshGroups]);

  const archiveGroup = useCallback(async (targetGroupId: string) => {
    const connection = connectionRef.current;
    if (!connection) throw new Error("Gateway is not ready");
    await connection.collaboration.archiveGroup(targetGroupId);
    refreshGroups();
  }, [refreshGroups]);

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

  const useAttachmentAgain = useCallback(async (attachment: MessageAttachmentState) => {
    if (!attachment.downloadUrl) return;
    const sessionId = stateRef.current.sessionId;
    const generation = stateRef.current.switchGeneration;
    try {
      const file = await readSessionFile(
        attachment.downloadUrl,
        attachment.name,
        attachment.mimeType,
      );
      if (
        stateRef.current.sessionId !== sessionId
        || stateRef.current.switchGeneration !== generation
      ) return;
      const requestId = ++attachmentRequestIdRef.current;
      setAttachmentsToQueue((current) => [...current, { file, requestId }]);
    } catch (error) {
      if (
        stateRef.current.sessionId !== sessionId
        || stateRef.current.switchGeneration !== generation
      ) return;
      dispatch({
        type: "error",
        message: error instanceof Error ? error.message : String(error),
      });
    }
  }, []);

  const stop = useCallback(() => {
    const sessionId = state.sessionId;
    const connection = connectionRef.current;
    if (!sessionId || !connection) return;
    void connection
      .stop(sessionId)
      .catch((error: Error) => dispatch({ type: "error", message: error.message }));
  }, [state.sessionId]);

  const switchChatModel = useCallback(
    async (
      registration: { model: string; provider: string },
      confirmExpensiveModel = false,
      persistGlobally = false,
    ) => {
      const sessionId = state.sessionId;
      const connection = connectionRef.current;
      if (!sessionId || !connection) throw new Error("No active conversation");
      if (state.isGenerating) {
        throw new Error("Stop the current response before switching chat models.");
      }
      return connection.switchModel(
        sessionId,
        registration.provider,
        registration.model,
        confirmExpensiveModel,
        persistGlobally,
      );
    },
    [state.isGenerating, state.sessionId],
  );

  const loadEarlier = useCallback(async () => {
    const sessionId = state.historySessionId;
    const cursor = state.historyCursor;
    const generation = state.switchGeneration;
    if (!sessionId || !cursor || state.historyLoading) return;
    historyAbortRef.current?.abort();
    const controller = new AbortController();
    historyAbortRef.current = controller;
    dispatch({ type: "history.prepend.started", generation, sessionId });
    const startedAt = performance.now();
    try {
      const response = await api.getSessionMessages(
        sessionId,
        { before: cursor, limit: 100, signal: controller.signal },
      );
      if (controller.signal.aborted) return;
      dispatch({ type: "history.prepend.succeeded", generation, response });
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
      dispatch({
        type: "history.prepend.failed",
        generation,
        message: error instanceof Error ? error.message : String(error),
        sessionId,
      });
    } finally {
      if (historyAbortRef.current === controller) historyAbortRef.current = null;
    }
  }, [state.historyCursor, state.historyLoading, state.historySessionId, state.switchGeneration]);

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
      query={sessionQuery}
      refreshNonce={sessionListRefreshNonce}
      sessionPath="/chat"
      variant="compact"
    />
  );
  const activeGroup = groups.find((group) => group.group_id === groupId);
  const conversationTitle = groupId
    ? activeGroup?.name ?? "Group"
    : activeSessionTitle ?? (activeSessionId ? "Conversation" : "New chat");
  const accountLabel = authMe?.display_name || authMe?.email || "Hermes workspace";
  const availableDirectEmployees = employees.filter(
    (employee) => employee.lifecycle_status === "active"
      && employee.profile !== null
      && employee.collaboration_policy.may_participate,
  );
  const employeeChatNotice = employeeLoadStatus === "loading"
    ? "AI employees are loading."
    : employeeLoadStatus === "error"
      ? "AI employees could not be loaded. Please refresh the page."
      : availableDirectEmployees.length === 0
        ? "No available AI employees. Please configure one in 员工管理."
        : null;
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
          aria-current={!workspacePaneOpen && !resumeSessionId ? "page" : undefined}
          className="gui-chat-nav-item"
          onClick={startNewGuiChat}
          type="button"
        >
          <MessageSquarePlus />
          <span>New chat</span>
        </button>
        <button
          aria-describedby={employeeChatNotice ? "employee-chat-notice" : undefined}
          aria-expanded={employeeChatOpen}
          aria-label="Start employee chat"
          className="gui-chat-nav-item disabled:cursor-not-allowed disabled:opacity-45"
          disabled={employeeChatNotice !== null}
          onClick={() => setEmployeeChatOpen((open) => !open)}
          type="button"
        >
          <Bot />
          <span>Chat with employee</span>
        </button>
        {employeeChatNotice ? (
          <p className="ml-6 px-3 pb-1 text-xs leading-5 text-red-600" id="employee-chat-notice" role="status">
            {employeeChatNotice}
          </p>
        ) : null}
        {employeeChatOpen ? (
          <div className="ml-6 space-y-[3px] border-l border-[#e4e6ea] pl-2">
            {availableDirectEmployees.map((employee) => (
              <button
                className="gui-chat-nav-item"
                key={employee.account_id}
                onClick={() => startEmployeeChat(employee.account_id)}
                type="button"
              >
                <span className="min-w-0 truncate">
                  {employee.profile?.name || employee.app_id}
                </span>
              </button>
            ))}
          </div>
        ) : null}
        <button
          aria-current={statisticsOpen ? "page" : undefined}
          aria-label="Message composition statistics"
          className="gui-chat-nav-item"
          onClick={() => {
            closeMobilePanel();
            navigate("/chat/statistics");
          }}
          type="button"
        >
          <PieChart />
          <span>Message statistics</span>
        </button>
        <button
          aria-current={filesOpen ? "page" : undefined}
          className="gui-chat-nav-item"
          onClick={() => {
            closeMobilePanel();
            navigate("/chat/files");
          }}
          type="button"
        >
          <FolderOpen />
          <span>Files</span>
        </button>
        <button
          aria-current={skillsOpen ? "page" : undefined}
          className="gui-chat-nav-item"
          onClick={() => {
            closeMobilePanel();
            navigate("/chat/skills");
          }}
          type="button"
        >
          <Sparkles />
          <span>Skills</span>
        </button>
        <button
          aria-current={scheduledTasksOpen ? "page" : undefined}
          className="gui-chat-nav-item"
          onClick={() => {
            closeMobilePanel();
            navigate("/chat/scheduled-tasks");
          }}
          type="button"
        >
          <CalendarClock />
          <span>Scheduled Tasks</span>
        </button>
        <button
          aria-current={robotsOpen ? "page" : undefined}
          aria-label="员工管理"
          className="gui-chat-nav-item"
          onClick={() => {
            closeMobilePanel();
            navigate("/chat/robots");
          }}
          type="button"
        >
          <Radio />
          <span>员工管理</span>
        </button>
        <button
          aria-current={modelsOpen ? "page" : undefined}
          aria-label="Manage models"
          className="gui-chat-nav-item"
          onClick={() => {
            closeMobilePanel();
            navigate("/chat/models");
          }}
          type="button"
        >
          <SlidersHorizontal />
          <span>Models</span>
        </button>
      </nav>
      <GroupsSidebar
        activeGroupId={groupId}
        groups={groups}
        loading={groupsLoading}
        onCreate={() => setCreateGroupOpen(true)}
        onPick={pickGroup}
        query={sessionQuery}
      />
      <div className="mt-4 flex min-h-0 flex-1 flex-col px-3">
        <div className="gui-chat-section-heading">
          <span>Recent chats</span>
          <button
            aria-label={t.common.refresh}
            className="gui-chat-icon-button"
            onClick={() => setSessionListRefreshNonce((nonce) => nonce + 1)}
            type="button"
          >
            <RefreshCw className="h-3.5 w-3.5" />
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
      {createGroupOpen ? (
        <CreateGroupDialog employees={employees} onClose={() => setCreateGroupOpen(false)} onCreate={createGroup} />
      ) : null}
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
            <h1 className="truncate text-[14px] font-medium leading-[22px] text-[#25282d]">
              {statisticsOpen
                ? "Message statistics"
                : filesOpen
                  ? "Files"
                : skillsOpen
                  ? "Skills"
                  : scheduledTasksOpen
                    ? "Scheduled Tasks"
                    : robotsOpen
                      ? "员工管理"
                      : modelsOpen
                        ? "Models"
                        : conversationTitle}
            </h1>
            <p className="truncate text-[0.625rem] text-[#969aa1]">
              {workspacePaneOpen
                ? "Workspace"
                : groupId
                  ? `${activeGroup?.status ?? "group"} · ${Object.values(groups).length} groups · ${state.connection}`
                  : `${state.model ?? "Hermes"} · ${mockMode ? "mock" : state.connection}`}
            </p>
          </div>
          {!workspacePaneOpen ? (
            <div className="ml-auto flex items-center gap-1">
              {groupId ? <UsersRound className="mr-1 h-3.5 w-3.5 text-[#777c84]" /> : null}
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
              {mockMode || state.connection !== "open" ? (
                <button aria-label={mockMode ? "Replay" : t.common.retry} className="gui-chat-icon-button" onClick={retryConnection} type="button">
                  <RefreshCw className={cn("h-3.5 w-3.5", state.connection === "connecting" && "animate-spin")} />
                </button>
              ) : null}
            </div>
          ) : null}
        </header>

        {statisticsOpen ? (
          <PageHeaderContext.Provider value={EMBEDDED_PAGE_HEADER}>
            <div
              data-statistics-pane
              data-theme="chat-workspace"
              className="gui-chat-statistics-pane min-h-0 flex-1 overflow-auto"
            >
              <SessionsPage />
            </div>
          </PageHeaderContext.Provider>
        ) : filesOpen ? (
          <GuiChatFilesPane />
        ) : skillsOpen ? (
          <GuiChatSkillsPane />
        ) : scheduledTasksOpen ? (
          <GuiChatScheduledTasksPane />
        ) : robotsOpen ? (
          <PageHeaderContext.Provider value={EMBEDDED_PAGE_HEADER}>
            <div
              data-robots-pane
              data-theme="chat-workspace"
              className="gui-chat-statistics-pane min-h-0 flex-1 overflow-auto"
            >
              <ChannelsPage />
            </div>
          </PageHeaderContext.Provider>
        ) : modelsOpen ? (
          <GuiChatModelsPane
            busy={state.isGenerating}
            canSwitchChat={Boolean(state.sessionId && state.connection === "open")}
            currentModel={state.model}
            currentProvider={state.provider}
            onSwitchChat={switchChatModel}
          />
        ) : groupId ? (
          <GroupChatView
            api={switchScope.coordinator.collaboration}
            connection={state.connection}
            employees={employees}
            groupId={groupId}
            onArchive={archiveGroup}
            onGroupChanged={refreshGroups}
          />
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
                onUseAttachmentAgain={useAttachmentAgain}
                state={state}
              />
              <Composer
                allowSendWhileGenerating={hasPendingClarification}
                attachmentToQueue={attachmentsToQueue[0]}
                disabled={disabled}
                isGenerating={state.isGenerating}
                modelPicker={
                  <ComposerModelPicker
                    busy={state.isGenerating}
                    canSwitch={Boolean(state.sessionId && state.connection === "open")}
                    currentModel={state.model}
                    currentProvider={state.provider}
                    onManageModels={() => navigate("/chat/models")}
                    onSwitchChat={(registration, confirmExpensiveModel) =>
                      switchChatModel(registration, confirmExpensiveModel)
                    }
                  />
                }
                onAttachmentQueued={(requestId) => {
                  setAttachmentsToQueue((current) =>
                    current.filter((request) => request.requestId !== requestId)
                  );
                }}
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
