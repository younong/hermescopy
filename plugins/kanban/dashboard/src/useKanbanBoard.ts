import { useCallback, useEffect, useRef, useState } from "./runtime";

import {
  kanbanApi,
  parseKanbanEventEnvelope,
  type KanbanApi,
  type KanbanBoardFilters,
} from "./api";
import { kanbanErrorMessage } from "./errors";
import {
  KANBAN_DEFAULT_BOARD,
  type KanbanBoardMetadata,
  type KanbanBoardResponse,
  type KanbanConnectionState,
  type KanbanTaskDetailResponse,
} from "./types";

const BOARD_STORAGE_KEY = "hermes.kanban.board";
const EVENT_REFRESH_DELAY_MS = 100;
const RECONNECT_MAX_DELAY_MS = 15_000;

type WebSocketFactory = (url: string) => WebSocket;
const defaultWebSocketFactory: WebSocketFactory = (url) => new WebSocket(url);

export interface UseKanbanBoardOptions {
  initialBoard?: string;
  initialFilters?: KanbanBoardFilters;
  api?: KanbanApi;
  webSocketFactory?: WebSocketFactory;
  reconnectDelayMs?: number;
}

function storedBoard(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(BOARD_STORAGE_KEY);
  } catch {
    return null;
  }
}

function saveBoard(board: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(BOARD_STORAGE_KEY, board);
  } catch {
    // Storage can be unavailable in private browsing; selection still works in memory.
  }
}

export function useKanbanBoard(options: UseKanbanBoardOptions = {}) {
  const client = options.api ?? kanbanApi;
  const socketFactory = options.webSocketFactory ?? defaultWebSocketFactory;
  const reconnectDelayMs = options.reconnectDelayMs ?? 1_000;

  const [boards, setBoards] = useState<KanbanBoardMetadata[]>([]);
  const [serverCurrentBoard, setServerCurrentBoard] = useState(KANBAN_DEFAULT_BOARD);
  const [activeBoard, setActiveBoardState] = useState(
    () => options.initialBoard || storedBoard() || KANBAN_DEFAULT_BOARD,
  );
  const [filters, setFilters] = useState<KanbanBoardFilters>(options.initialFilters ?? {});
  const [board, setBoard] = useState<KanbanBoardResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [selectedTask, setSelectedTask] = useState<KanbanTaskDetailResponse | null>(null);
  const [selectedTaskLoading, setSelectedTaskLoading] = useState(false);
  const [selectedTaskError, setSelectedTaskError] = useState<string | null>(null);
  const [connectionState, setConnectionState] = useState<KanbanConnectionState>("idle");
  const [socketError, setSocketError] = useState<string | null>(null);

  const mountedRef = useRef(true);
  const activeBoardRef = useRef(activeBoard);
  const selectedTaskIdRef = useRef(selectedTaskId);
  const boardRequestRef = useRef(0);
  const boardsRequestRef = useRef(0);
  const detailRequestRef = useRef(0);
  const cursorRef = useRef(0);
  const refreshRef = useRef<() => Promise<void>>(async () => undefined);
  const refreshDetailRef = useRef<() => Promise<void>>(async () => undefined);

  const refreshBoards = useCallback(async () => {
    const request = ++boardsRequestRef.current;
    try {
      const response = await client.listBoards(false);
      if (!mountedRef.current || request !== boardsRequestRef.current) return;
      setBoards(response.boards);
      setServerCurrentBoard(response.current || KANBAN_DEFAULT_BOARD);
      if (!response.boards.some((candidate) => candidate.slug === activeBoardRef.current)) {
        const fallback = response.boards.some((candidate) => candidate.slug === response.current)
          ? response.current
          : (response.boards[0]?.slug ?? KANBAN_DEFAULT_BOARD);
        activeBoardRef.current = fallback;
        setActiveBoardState(fallback);
        saveBoard(fallback);
      }
    } catch (nextError) {
      if (!mountedRef.current || request !== boardsRequestRef.current) return;
      setError(kanbanErrorMessage(nextError));
    }
  }, [client]);

  const refresh = useCallback(async () => {
    const request = ++boardRequestRef.current;
    const requestedBoard = activeBoard || KANBAN_DEFAULT_BOARD;
    setLoading(true);
    setError(null);
    try {
      const response = await client.getBoard(requestedBoard, filters);
      if (!mountedRef.current || request !== boardRequestRef.current) return;
      setBoard(response);
      cursorRef.current = Math.max(cursorRef.current, response.latest_event_id);
    } catch (nextError) {
      if (!mountedRef.current || request !== boardRequestRef.current) return;
      setError(kanbanErrorMessage(nextError));
    } finally {
      if (mountedRef.current && request === boardRequestRef.current) setLoading(false);
    }
  }, [activeBoard, client, filters]);

  const refreshSelectedTask = useCallback(async () => {
    if (!selectedTaskId) {
      detailRequestRef.current += 1;
      setSelectedTask(null);
      setSelectedTaskError(null);
      setSelectedTaskLoading(false);
      return;
    }
    const request = ++detailRequestRef.current;
    const requestedBoard = activeBoard || KANBAN_DEFAULT_BOARD;
    const requestedTask = selectedTaskId;
    setSelectedTaskLoading(true);
    setSelectedTaskError(null);
    try {
      const response = await client.getTask(requestedBoard, requestedTask);
      if (!mountedRef.current || request !== detailRequestRef.current) return;
      setSelectedTask(response);
    } catch (nextError) {
      if (!mountedRef.current || request !== detailRequestRef.current) return;
      setSelectedTaskError(kanbanErrorMessage(nextError));
    } finally {
      if (mountedRef.current && request === detailRequestRef.current) {
        setSelectedTaskLoading(false);
      }
    }
  }, [activeBoard, client, selectedTaskId]);

  useEffect(() => {
    refreshRef.current = refresh;
    refreshDetailRef.current = refreshSelectedTask;
    selectedTaskIdRef.current = selectedTaskId;
  }, [refresh, refreshSelectedTask, selectedTaskId]);

  const setActiveBoard = useCallback((nextBoard: string) => {
    const normalized = nextBoard || KANBAN_DEFAULT_BOARD;
    activeBoardRef.current = normalized;
    setActiveBoardState(normalized);
    saveBoard(normalized);
  }, []);

  const runMutation = useCallback(async <T,>(
    operation: (api: KanbanApi, board: string) => Promise<T>,
    refreshAfter = true,
  ): Promise<T> => {
    const result = await operation(client, activeBoard || KANBAN_DEFAULT_BOARD);
    if (refreshAfter) {
      await Promise.all([refreshRef.current(), refreshDetailRef.current()]);
    }
    return result;
  }, [activeBoard, client]);

  useEffect(() => {
    mountedRef.current = true;
    // Existing dashboard hooks use an effect for their initial authenticated load.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refreshBoards();
    return () => {
      mountedRef.current = false;
      boardsRequestRef.current += 1;
      boardRequestRef.current += 1;
      detailRequestRef.current += 1;
    };
  }, [refreshBoards]);

  useEffect(() => {
    boardRequestRef.current += 1;
    detailRequestRef.current += 1;
    cursorRef.current = 0;
    // Board identity changes invalidate all feature-local snapshots immediately.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setBoard(null);
    setSelectedTask(null);
    setSelectedTaskId(null);
    void refresh();
  }, [activeBoard, filters, refresh]);

  useEffect(() => {
    // Selection changes load the matching task detail snapshot.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refreshSelectedTask();
  }, [refreshSelectedTask]);

  useEffect(() => {
    let disposed = false;
    let socket: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let eventRefreshTimer: ReturnType<typeof setTimeout> | null = null;
    let refreshSelectedTaskAfterEvents = false;
    let reconnectAttempt = 0;
    let connectionAbort: AbortController | null = null;

    const scheduleRefresh = (refreshSelectedTask: boolean) => {
      refreshSelectedTaskAfterEvents ||= refreshSelectedTask;
      if (eventRefreshTimer !== null) return;
      eventRefreshTimer = setTimeout(() => {
        eventRefreshTimer = null;
        const operations = [refreshRef.current()];
        if (refreshSelectedTaskAfterEvents) operations.push(refreshDetailRef.current());
        refreshSelectedTaskAfterEvents = false;
        void Promise.all(operations);
      }, EVENT_REFRESH_DELAY_MS);
    };

    const scheduleReconnect = () => {
      if (disposed || reconnectTimer !== null) return;
      setConnectionState("reconnecting");
      const delay = Math.min(
        RECONNECT_MAX_DELAY_MS,
        reconnectDelayMs * 2 ** Math.min(reconnectAttempt, 4),
      );
      reconnectAttempt += 1;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        void connect();
      }, delay);
    };

    const connect = async () => {
      if (disposed) return;
      connectionAbort?.abort();
      connectionAbort = new AbortController();
      setConnectionState(reconnectAttempt === 0 ? "connecting" : "reconnecting");
      setSocketError(null);
      try {
        const url = await client.buildEventsUrl(
          activeBoard || KANBAN_DEFAULT_BOARD,
          cursorRef.current,
          connectionAbort.signal,
        );
        if (disposed) return;
        socket = socketFactory(url);
        socket.onopen = () => {
          if (disposed) return;
          reconnectAttempt = 0;
          setConnectionState("connected");
          setSocketError(null);
        };
        socket.onmessage = (message) => {
          if (disposed) return;
          const envelope = parseKanbanEventEnvelope(message.data);
          if (!envelope) return;
          cursorRef.current = Math.max(cursorRef.current, envelope.cursor);
          if (envelope.events.length > 0) {
            scheduleRefresh(envelope.events.some((event) => event.task_id === selectedTaskIdRef.current));
          }
        };
        socket.onerror = () => {
          if (!disposed) setSocketError("Kanban live updates disconnected");
        };
        socket.onclose = () => {
          socket = null;
          scheduleReconnect();
        };
      } catch (nextError) {
        if (disposed || connectionAbort.signal.aborted) return;
        setSocketError(kanbanErrorMessage(nextError));
        scheduleReconnect();
      }
    };

    void connect();
    return () => {
      disposed = true;
      connectionAbort?.abort();
      if (reconnectTimer !== null) clearTimeout(reconnectTimer);
      if (eventRefreshTimer !== null) clearTimeout(eventRefreshTimer);
      if (socket) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onerror = null;
        socket.onclose = null;
        socket.close();
      }
      setConnectionState("idle");
    };
  }, [activeBoard, client, reconnectDelayMs, socketFactory]);

  return {
    activeBoard,
    board,
    boards,
    connectionState,
    error,
    filters,
    loading,
    refresh,
    refreshBoards,
    refreshSelectedTask,
    runMutation,
    selectedTask,
    selectedTaskError,
    selectedTaskId,
    selectedTaskLoading,
    serverCurrentBoard,
    setActiveBoard,
    setFilters,
    setSelectedTaskId,
    socketError,
  };
}
