import { useVirtualizer } from "@tanstack/react-virtual";
import { useCallback, useLayoutEffect, useMemo, useRef } from "react";
import type { UIEvent } from "react";
import { useLoadEarlierOnScroll } from "@/hooks/useLoadEarlierOnScroll";
import type { ArtifactState, GuiChatState } from "../types";
import { ApprovalCard } from "./ApprovalCard";
import { ArtifactCard } from "./ArtifactCard";
import { ClarifyCard } from "./ClarifyCard";
import { MessageBubble } from "./MessageBubble";

const BOTTOM_THRESHOLD_PX = 64;
const OVERSCAN_ROWS = 12;

type RenderRow =
  | { id: string; kind: "history" }
  | { id: string; kind: "message"; messageId: string }
  | { artifact: ArtifactState; id: string; kind: "artifact" }
  | { approvalId: string; id: string; kind: "approval" }
  | { clarificationId: string; id: string; kind: "clarify" }
  | { id: string; kind: "status" };

function isNearBottom(element: HTMLElement): boolean {
  return element.scrollHeight - element.scrollTop - element.clientHeight <= BOTTOM_THRESHOLD_PX;
}

export function MessageList({
  disabled,
  forceBottomKey,
  onApprovalRespond,
  onClarifyRespond,
  onLoadEarlier,
  state,
}: {
  disabled?: boolean;
  forceBottomKey?: string;
  onApprovalRespond: (id: string, approved: boolean) => void;
  onClarifyRespond: (id: string, answer: string) => void;
  onLoadEarlier?: () => void;
  state: GuiChatState;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const followBottomRef = useRef(true);
  const lastForceBottomKeyRef = useRef<string | undefined>(undefined);
  const anchorRef = useRef<{ id: string; offset: number } | null>(null);

  const rows = useMemo<RenderRow[]>(() => {
    const result: RenderRow[] = [];
    if (state.historyHasMore || state.historyLoading || state.historyError || state.safeguardReached) {
      result.push({ id: "history-control", kind: "history" });
    }
    for (const message of state.messages) {
      result.push({ id: `message:${message.id}`, kind: "message", messageId: message.id });
    }
    for (const id of state.toolOrder) {
      const tool = state.toolCalls[id];
      for (const artifactId of tool?.artifactIds ?? []) {
        const artifact = state.artifacts[artifactId];
        if (artifact) result.push({ artifact, id: `artifact:${artifact.id}`, kind: "artifact" });
      }
    }
    for (const approvalId of state.approvalOrder) {
      if (state.approvals[approvalId]) {
        result.push({ approvalId, id: `approval:${approvalId}`, kind: "approval" });
      }
    }
    for (const clarificationId of state.clarificationOrder) {
      if (state.clarifications[clarificationId]) {
        result.push({
          clarificationId,
          id: `clarify:${clarificationId}`,
          kind: "clarify",
        });
      }
    }
    if (state.statusLines.length > 0) result.push({ id: "status", kind: "status" });
    return result;
  }, [state]);

  const virtualizer = useVirtualizer({
    count: rows.length,
    estimateSize: (index) => rows[index]?.kind === "message" ? 140 : 72,
    getItemKey: (index) => rows[index]?.id ?? index,
    getScrollElement: () => containerRef.current,
    initialRect: { height: 600, width: 800 },
    overscan: OVERSCAN_ROWS,
  });

  const captureAnchor = useCallback(() => {
    const container = containerRef.current;
    if (!container) return;
    const firstVisible = virtualizer.getVirtualItems().find((item) => {
      const row = rows[item.index];
      return row?.kind === "message" && item.end > container.scrollTop;
    });
    const row = firstVisible ? rows[firstVisible.index] : undefined;
    if (firstVisible && row) {
      anchorRef.current = { id: row.id, offset: firstVisible.start - container.scrollTop };
      followBottomRef.current = false;
    }
  }, [rows, virtualizer]);

  const { handleScroll: handleHistoryScroll, retry, syncScrollPosition } =
    useLoadEarlierOnScroll({
      autoEnabled: !state.historyError && !state.safeguardReached,
      canLoad: state.historyHasMore && !!state.historyCursor,
      loading: state.historyLoading,
      onBeforeLoad: captureAnchor,
      onLoadEarlier,
      resetKey: state.sessionId,
    });

  const scrollToBottom = useCallback((force = false) => {
    if (force) followBottomRef.current = true;
    if (!followBottomRef.current || rows.length === 0) return;
    virtualizer.scrollToIndex(rows.length - 1, { align: "end" });
    const element = containerRef.current;
    if (element) syncScrollPosition(element.scrollTop);
  }, [rows.length, syncScrollPosition, virtualizer]);

  const handleScroll = useCallback((event: UIEvent<HTMLDivElement>) => {
    followBottomRef.current = isNearBottom(event.currentTarget);
    handleHistoryScroll(event);
  }, [handleHistoryScroll]);

  useLayoutEffect(() => {
    if (forceBottomKey === lastForceBottomKeyRef.current) return;
    lastForceBottomKeyRef.current = forceBottomKey;
    scrollToBottom(true);
  }, [forceBottomKey, scrollToBottom]);

  useLayoutEffect(() => {
    if (!state.historyLoading && anchorRef.current) {
      const anchor = anchorRef.current;
      const index = rows.findIndex((row) => row.id === anchor.id);
      if (index >= 0) {
        virtualizer.scrollToIndex(index, { align: "start" });
        const element = containerRef.current;
        if (element) {
          element.scrollTop -= anchor.offset;
          syncScrollPosition(element.scrollTop);
        }
      }
      anchorRef.current = null;
    }
    if (followBottomRef.current) scrollToBottom();
  }, [rows, scrollToBottom, state.historyLoading, syncScrollPosition, virtualizer]);

  if (rows.length === 0) {
    return (
      <div className="flex min-h-0 flex-1 overflow-y-auto px-3 py-4 sm:px-5">
        <div className="m-auto max-w-xl border border-current/15 bg-midground/5 px-6 py-5 text-center">
          <h2 className="mb-2 font-display text-lg uppercase tracking-[0.12em] text-midground">Hermes GUI Chat beta</h2>
          <p className="text-sm text-text-secondary">Structured chat over /api/ws. Terminal Chat remains available at /chat.</p>
        </div>
      </div>
    );
  }

  return (
    <div aria-busy={state.historyLoading} className="min-h-0 flex-1 overflow-y-auto px-3 py-4 sm:px-5" onScroll={handleScroll} ref={containerRef}>
      <div className="relative w-full" style={{ height: `${virtualizer.getTotalSize()}px` }}>
        {virtualizer.getVirtualItems().map((item) => {
          const row = rows[item.index];
          if (!row) return null;
          return (
            <div
              className="absolute left-0 top-0 w-full pb-4"
              data-index={item.index}
              key={row.id}
              ref={virtualizer.measureElement}
              style={{ transform: `translateY(${item.start}px)` }}
            >
              {row.kind === "history" ? (
                <div className="flex min-h-9 flex-col items-center justify-center gap-2 text-xs text-text-tertiary">
                  {state.safeguardReached ? <span>Earlier history remains on the server; this tab stopped loading it to stay responsive.</span> : null}
                  {state.historyError ? (
                    <>
                      <span role="alert">{state.historyError}</span>
                      {state.historyHasMore && !state.safeguardReached ? (
                        <button className="border border-current/20 px-3 py-1.5 hover:bg-midground/5 disabled:opacity-50" disabled={state.historyLoading} onClick={retry} type="button">
                          Retry loading earlier messages
                        </button>
                      ) : null}
                    </>
                  ) : state.historyLoading ? (
                    <span aria-live="polite" role="status">Loading earlier messages…</span>
                  ) : state.historyHasMore && !state.safeguardReached ? (
                    <span>Scroll up for earlier messages</span>
                  ) : null}
                </div>
              ) : row.kind === "message" ? (() => {
                const message = state.messages.find((value) => value.id === row.messageId);
                return message ? <MessageBubble artifacts={message.artifactIds.map((id) => state.artifacts[id]).filter(Boolean)} message={message} /> : null;
              })() : row.kind === "artifact" ? (
                <ArtifactCard artifact={row.artifact} />
              ) : row.kind === "approval" ? (() => {
                const approval = state.approvals[row.approvalId];
                return approval ? <ApprovalCard approval={approval} disabled={disabled} onRespond={(approved) => onApprovalRespond(row.approvalId, approved)} /> : null;
              })() : row.kind === "clarify" ? (() => {
                const clarification = state.clarifications[row.clarificationId];
                return clarification ? (
                  <ClarifyCard
                    clarification={clarification}
                    disabled={disabled}
                    onRespond={(answer) => onClarifyRespond(row.clarificationId, answer)}
                  />
                ) : null;
              })() : (
                <div className="space-y-1 text-xs text-text-tertiary">
                  {state.statusLines.slice(-3).map((line, index) => <div className="truncate" key={`${index}-${line}`}>{line}</div>)}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
