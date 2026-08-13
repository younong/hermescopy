/**
 * ChatSessionList — a ChatGPT-style conversation switcher for dashboard chat
 * surfaces.
 *
 * It lists the authenticated Owner's most recent sessions and
 * lets the user swap between them without leaving the current chat surface.
 * Selecting a row sets the current route's `?resume=<id>` query param; the
 * mounted chat surface treats that resume target as part of its connection
 * identity and reconnects/resumes as needed. The "New session" action clears
 * the resume param and can delegate to the surface's own force-fresh handler.
 *
 * Best-effort, like ChatSidebar: a failed fetch surfaces a small inline
 * error with a retry affordance and the active chat pane keeps working.
 *
 * This stays a focused navigation surface: users can select, create, rename,
 * and remove conversations from the list. Removal archives the stored session,
 * preserving its messages instead of physically deleting data.
 */

import { Button } from "@nous-research/ui/ui/components/button";
import { Input } from "@nous-research/ui/ui/components/input";
import { ListItem } from "@nous-research/ui/ui/components/list-item";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { useConfirmDelete } from "@nous-research/ui/hooks/use-confirm-delete";
import { AlertCircle, Check, MessageSquarePlus, Pencil, RefreshCw, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { DeleteConfirmDialog } from "@/components/DeleteConfirmDialog";
import { sessionSoftDeleteMessage, useI18n } from "@/i18n";
import { api, type SessionInfo } from "@/lib/api";
import { cn, timeAgo } from "@/lib/utils";

const SESSION_LIMIT = 30;
interface RenameState {
  error: string | null;
  id: string | null;
  saving: boolean;
  value: string;
}
const EMPTY_RENAME: RenameState = {
  error: null,
  id: null,
  saving: false,
  value: "",
};

interface ChatSessionListProps {
  /** Active resume target (the session currently shown by the chat surface). */
  activeSessionId: string | null;
  className?: string;
  /** Optional local title/preview filter used by compact chat sidebars. */
  query?: string;
  /** External trigger for the list's existing reload mechanism. */
  refreshNonce?: number;
  /** Keep the original rich panel by default; compact is a single-line list. */
  variant?: "default" | "compact";
  /** Reports the loaded active row so a chat shell can mirror its title. */
  onActiveSessionChange?: (session: { id: string; label: string } | null) => void;
  /** Optional callback fired after a row is picked (e.g. close mobile sheet). */
  onPicked?: () => void;
  /** Route to open before applying a selected session's resume query. */
  sessionPath?: string;
  /** Called before navigation so the owning surface can start an end-to-end trace. */
  onSessionPick?: (id: string) => void;
  /**
   * Starts a fresh chat. Chat surfaces can supply a handler that clears
   * `?resume` AND bumps their reconnect nonce so a brand-new session starts
   * even when the user is already on an unsaved fresh conversation. When
   * omitted, we fall back to clearing the resume param ourselves.
   */
  onNewChat?: () => void;
}

function rowLabel(session: SessionInfo, untitled: string): string {
  const title = session.title?.trim();
  if (title && title !== "Untitled") return title;
  const preview = session.preview?.trim();
  if (preview) return preview;
  return untitled;
}

export function ChatSessionList({
  activeSessionId,
  className,
  query = "",
  refreshNonce = 0,
  variant = "default",
  onActiveSessionChange,
  onPicked,
  onSessionPick,
  onNewChat,
  sessionPath,
}: ChatSessionListProps) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const [, setSearchParams] = useSearchParams();
  const [sessions, setSessions] = useState<SessionInfo[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [rename, setRename] = useState<RenameState>(EMPTY_RENAME);
  // Bumped to force a refetch (after switching, on Refresh, on mount).
  const [reloadNonce, setReloadNonce] = useState(0);

  // Monotonic request tokens ignore stale list loads and title updates.
  const reqRef = useRef(0);
  const renameReqRef = useRef(0);
  const loadErrorFallbackRef = useRef(t.sessions.noSessions);
  loadErrorFallbackRef.current = t.sessions.noSessions;

  const load = useCallback(() => {
    const myReq = ++reqRef.current;
    setLoading(true);
    setError(null);
    api
      .getSessions(SESSION_LIMIT, 0, "recent", variant === "compact")
      .then((res) => {
        if (reqRef.current !== myReq) return;
        setSessions(res.sessions);
      })
      .catch((e: Error) => {
        if (reqRef.current !== myReq) return;
        setError(e.message || loadErrorFallbackRef.current);
      })
      .finally(() => {
        if (reqRef.current === myReq) setLoading(false);
      });
  }, [variant]);

  useEffect(() => {
    // Dashboard data surfaces fetch from an effect on mount + scope change;
    // keep this local and explicit (matches FilesPage).
    load();
    // The internal nonce powers this component's own retry button; the external
    // nonce lets a compact parent expose the same list-only refresh behavior.
  }, [load, refreshNonce, reloadNonce]);

  const reload = useCallback(() => setReloadNonce((n) => n + 1), []);
  const filteredSessions = useMemo(() => {
    if (!sessions) return sessions;
    const normalizedQuery = query.trim().toLocaleLowerCase();
    if (!normalizedQuery) return sessions;
    return sessions.filter((session) =>
      `${session.title ?? ""}\n${session.preview ?? ""}`
        .toLocaleLowerCase()
        .includes(normalizedQuery),
    );
  }, [query, sessions]);

  useEffect(() => {
    if (!onActiveSessionChange) return;
    const active = sessions?.find((session) => session.id === activeSessionId);
    onActiveSessionChange(
      active
        ? { id: active.id, label: rowLabel(active, t.sessions.untitledSession) }
        : null,
    );
  }, [activeSessionId, onActiveSessionChange, sessions, t.sessions.untitledSession]);

  // Picking a row sets the current route's `?resume=<id>`. Re-picking the row
  // already shown by the chat surface is a no-op (avoids a needless reconnect).
  const pick = useCallback(
    (id: string) => {
      onPicked?.();
      if (id === activeSessionId) return;
      onSessionPick?.(id);
      if (sessionPath) {
        const next = new URLSearchParams();
        next.set("resume", id);
        navigate(`${sessionPath}?${next.toString()}`);
        return;
      }
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set("resume", id);
          return next;
        },
        { replace: false },
      );
    },
    [activeSessionId, navigate, onPicked, onSessionPick, sessionPath, setSearchParams],
  );

  const cancelRename = useCallback(() => setRename(EMPTY_RENAME), []);

  const beginRename = useCallback((session: SessionInfo) => {
    const title = session.title?.trim();
    setRename({
      error: null,
      id: session.id,
      saving: false,
      value: title && title !== "Untitled" ? title : "",
    });
  }, []);

  const submitRename = useCallback(
    async (session: SessionInfo) => {
      if (rename.saving) return;
      const title = rename.value.trim();
      const currentTitle = session.title?.trim() ?? "";
      if (!title || title === currentTitle) {
        cancelRename();
        return;
      }

      const myReq = ++renameReqRef.current;
      setRename((current) => ({ ...current, error: null, saving: true }));
      try {
        const result = await api.renameSession(session.id, title);
        if (renameReqRef.current !== myReq) return;
        reqRef.current += 1;
        setSessions((current) =>
          current?.map((item) =>
            item.id === session.id ? { ...item, title: result.title } : item,
          ) ?? null,
        );
        setRename(EMPTY_RENAME);
      } catch (cause) {
        if (renameReqRef.current !== myReq) return;
        setRename((current) => ({
          ...current,
          error:
            cause instanceof Error && cause.message
              ? cause.message
              : t.sessions.failedToRename!,
        }));
      } finally {
        if (renameReqRef.current === myReq) {
          setRename((current) => ({ ...current, saving: false }));
        }
      }
    },
    [cancelRename, rename.saving, rename.value, t.sessions.failedToRename],
  );

  // "New chat" prefers the owning chat surface's robust handler (clears resume
  // + forces a fresh connection even from an already-fresh session). Fallback:
  // clear the resume param ourselves, which starts a fresh session whenever one
  // was being resumed.
  const startNew = useCallback(() => {
    onPicked?.();
    if (onNewChat) {
      onNewChat();
      return;
    }
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.delete("resume");
        return next;
      },
      { replace: false },
    );
  }, [onNewChat, onPicked, setSearchParams]);

  const {
    cancel: cancelDelete,
    confirm: confirmDelete,
    isDeleting,
    isOpen: deleteOpen,
    pendingId: pendingDeleteId,
    requestDelete,
  } = useConfirmDelete({
    onDelete: useCallback(
      async (id: string) => {
        setDeleteError(null);
        try {
          await api.archiveSession(id);
          reqRef.current += 1;
          setSessions((current) => current?.filter((session) => session.id !== id) ?? null);
          if (id === activeSessionId) startNew();
        } catch (cause) {
          setDeleteError(
            cause instanceof Error && cause.message
              ? cause.message
              : t.sessions.failedToDelete,
          );
          throw cause;
        }
      },
      [activeSessionId, startNew, t.sessions.failedToDelete],
    ),
  });

  const content = useMemo(() => {
    if (loading && sessions === null) {
      return (
        <div className="flex items-center justify-center gap-2 px-2 py-6 text-xs text-text-secondary">
          <Spinner /> {t.common.loading}
        </div>
      );
    }
    if (error) {
      return (
        <div className="flex flex-col items-start gap-2 px-2 py-4 text-xs">
          <div className="flex items-start gap-2 text-destructive">
            <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span className="wrap-break-word">{error}</span>
          </div>
          <Button size="sm" outlined onClick={reload} prefix={<RefreshCw />}>
            {t.common.retry}
          </Button>
        </div>
      );
    }
    if (!sessions || sessions.length === 0) {
      return (
        <div className="px-2 py-6 text-center text-xs text-text-secondary">
          {t.sessions.noSessions}
        </div>
      );
    }
    if (!filteredSessions || filteredSessions.length === 0) {
      return (
        <div className="px-2 py-6 text-center text-xs text-text-secondary">
          {t.sessions.noMatch}
        </div>
      );
    }
    return (
      <div className={cn("flex flex-col", variant === "compact" ? "gap-[3px]" : "gap-0.5")}>
        {filteredSessions.map((s) => {
          const isActive = s.id === activeSessionId;
          const isRenaming = s.id === rename.id;
          const label = rowLabel(s, t.sessions.untitledSession);
          const renameLabel = t.sessions.renameSession!;
          const titlePlaceholder = t.sessions.sessionTitlePlaceholder!;

          if (isRenaming) {
            return (
              <div
                key={s.id}
                aria-busy={rename.saving}
                aria-current={isActive ? "true" : undefined}
                className={cn(
                  "rounded",
                  variant === "compact"
                    ? "bg-white px-1 py-1 text-black"
                    : isActive
                      ? "border-l-2 border-primary bg-primary/10 px-1 py-1"
                      : "px-1 py-1",
                )}
              >
                <div className="flex min-w-0 items-center gap-1">
                  <Input
                    autoFocus
                    value={rename.value}
                    onChange={(event) =>
                      setRename((current) => ({
                        ...current,
                        value: event.target.value,
                      }))
                    }
                    onKeyDown={(event) => {
                      event.stopPropagation();
                      if (event.nativeEvent.isComposing) return;
                      if (event.key === "Enter") {
                        event.preventDefault();
                        void submitRename(s);
                      } else if (event.key === "Escape") {
                        event.preventDefault();
                        cancelRename();
                      }
                    }}
                    onClick={(event) => event.stopPropagation()}
                    placeholder={titlePlaceholder}
                    aria-label={titlePlaceholder}
                    className={cn(
                      "h-8 min-w-0 flex-1 bg-transparent py-0",
                      variant === "compact" ? "text-[14px] font-medium leading-[22px]" : "text-sm",
                    )}
                    disabled={rename.saving}
                  />
                  <Button
                    ghost
                    size="icon"
                    className="shrink-0 text-text-secondary hover:text-success"
                    aria-label={t.common.save}
                    title={t.common.save}
                    disabled={rename.saving}
                    onClick={(event) => {
                      event.stopPropagation();
                      void submitRename(s);
                    }}
                  >
                    {rename.saving ? <Spinner className="text-sm" /> : <Check />}
                  </Button>
                  <Button
                    ghost
                    size="icon"
                    className="shrink-0 text-text-secondary hover:text-foreground"
                    aria-label={t.common.cancel}
                    title={t.common.cancel}
                    disabled={rename.saving}
                    onClick={(event) => {
                      event.stopPropagation();
                      cancelRename();
                    }}
                  >
                    <X />
                  </Button>
                </div>
                {rename.error ? (
                  <div role="alert" className="px-1 pt-1 text-xs text-destructive">
                    {rename.error}
                  </div>
                ) : null}
              </div>
            );
          }

          return (
            <div key={s.id} className="group relative min-w-0">
              <ListItem
                onClick={() => pick(s.id)}
                aria-current={isActive ? "true" : undefined}
                className={cn(
                  "flex-col items-start pr-[4.25rem] normal-case tracking-normal",
                  variant === "compact"
                    ? "min-h-9 gap-0 rounded-[10px] px-2 py-[7px] text-[#1f1f1f]"
                    : "gap-0.5 rounded px-2 py-1.5",
                  variant === "compact"
                    ? isActive
                      ? "bg-white text-black"
                      : "hover:bg-black/[0.04] hover:text-black"
                    : isActive
                      ? "border-l-2 border-primary bg-primary/10 text-foreground"
                      : "text-text-secondary hover:bg-midground/5 hover:text-foreground",
                )}
              >
                <span className={cn("w-full truncate", variant === "compact" ? "text-[14px] font-medium leading-[22px]" : "text-sm font-medium")}>
                  {label}
                </span>
                {variant === "default" ? (
                  <span className="flex w-full items-center gap-1.5 text-[0.6875rem] text-text-tertiary">
                    <span>{timeAgo(s.last_active)}</span>
                    {s.message_count > 0 && (
                      <>
                        <span aria-hidden>·</span>
                        <span>{s.message_count} {t.common.msgs}</span>
                      </>
                    )}
                    {s.source && s.source !== "cli" && (
                      <>
                        <span aria-hidden>·</span>
                        <span className="truncate">{s.source}</span>
                      </>
                    )}
                  </span>
                ) : null}
              </ListItem>
              <div
                className={cn(
                  "absolute right-1 top-1/2 flex -translate-y-1/2 opacity-0 transition-opacity",
                  "focus-within:opacity-100 group-hover:opacity-100",
                  isActive && "opacity-100",
                )}
              >
                <Button
                  ghost
                  size="icon"
                  className="h-7 w-7 text-text-secondary hover:text-foreground"
                  aria-label={`${renameLabel}: ${label}`}
                  title={renameLabel}
                  onClick={(event) => {
                    event.stopPropagation();
                    beginRename(s);
                  }}
                >
                  <Pencil />
                </Button>
                <Button
                  ghost
                  size="icon"
                  className="h-7 w-7 text-text-secondary hover:text-destructive"
                  aria-label={`${t.sessions.deleteSession}: ${label}`}
                  title={t.sessions.deleteSession}
                  onClick={(event) => {
                    event.stopPropagation();
                    setDeleteError(null);
                    requestDelete(s.id);
                  }}
                >
                  <Trash2 />
                </Button>
              </div>
            </div>
          );
        })}
      </div>
    );
  }, [
    activeSessionId,
    beginRename,
    cancelRename,
    error,
    filteredSessions,
    loading,
    pick,
    reload,
    rename,
    requestDelete,
    sessions,
    submitRename,
    t,
    variant,
  ]);

  const pendingDelete = sessions?.find((session) => session.id === pendingDeleteId) ?? null;
  const softDeleteMessage = sessionSoftDeleteMessage(t);
  const deleteDescription = pendingDelete
    ? `${rowLabel(pendingDelete, t.sessions.untitledSession)} — ${softDeleteMessage}`
    : softDeleteMessage;

  return (
    <>
      <DeleteConfirmDialog
        open={deleteOpen}
        onCancel={cancelDelete}
        onConfirm={() => void confirmDelete()}
        title={t.sessions.confirmDeleteTitle}
        description={deleteError ?? deleteDescription}
        loading={isDeleting}
      />
      <aside
        className={cn(
          "flex h-full w-full min-w-0 shrink-0 flex-col overflow-hidden",
          className,
        )}
      >
        {variant === "default" ? (
          <>
            <div className="flex items-center justify-between gap-2 px-2 pb-2">
              <span className="text-display text-xs tracking-wider text-text-tertiary">
                {t.sessions.title}
              </span>
              <Button
                ghost
                size="icon"
                onClick={reload}
                aria-label={t.common.refresh}
                title={t.common.refresh}
                className="text-text-secondary hover:text-foreground"
              >
                <RefreshCw className={cn(loading && "animate-spin")} />
              </Button>
            </div>

            <Button
              outlined
              size="sm"
              onClick={startNew}
              prefix={<MessageSquarePlus />}
              className="mx-2 mb-2 justify-center"
            >
              {t.sessions.newChat}
            </Button>
          </>
        ) : null}

        <div className={cn("min-h-0 flex-1 overflow-y-auto overflow-x-hidden pb-1", variant === "default" ? "px-1" : "px-0.5")}>
          {content}
        </div>
      </aside>
    </>
  );
}
