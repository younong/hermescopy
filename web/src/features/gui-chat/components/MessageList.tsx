import { useCallback, useEffect, useLayoutEffect, useRef } from "react";
import type { UIEvent } from "react";
import type { GuiChatState } from "../types";
import { ApprovalCard } from "./ApprovalCard";
import { MessageBubble } from "./MessageBubble";
import { ToolCallCard } from "./ToolCallCard";

const BOTTOM_THRESHOLD_PX = 64;

function isNearBottom(element: HTMLElement): boolean {
  return (
    element.scrollHeight - element.scrollTop - element.clientHeight <=
    BOTTOM_THRESHOLD_PX
  );
}

export function MessageList({
  disabled,
  forceBottomKey,
  onApprovalRespond,
  state,
}: {
  disabled?: boolean;
  forceBottomKey?: string;
  onApprovalRespond: (id: string, approved: boolean) => void;
  state: GuiChatState;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const contentRef = useRef<HTMLDivElement | null>(null);
  const followBottomRef = useRef(true);
  const lastForceBottomKeyRef = useRef<string | undefined>(undefined);
  const scrollFrameRef = useRef<number | undefined>(undefined);

  const scrollToBottom = useCallback((options?: { force?: boolean }) => {
    const container = containerRef.current;
    if (!container) return;
    if (options?.force) {
      followBottomRef.current = true;
    } else if (!followBottomRef.current) {
      return;
    }
    container.scrollTop = container.scrollHeight;
  }, []);

  const scheduleScrollToBottom = useCallback(
    (options?: { force?: boolean }) => {
      if (options?.force) {
        followBottomRef.current = true;
      } else if (!followBottomRef.current) {
        return;
      }
      if (scrollFrameRef.current !== undefined) return;
      scrollFrameRef.current = requestAnimationFrame(() => {
        scrollFrameRef.current = undefined;
        scrollToBottom(options);
      });
    },
    [scrollToBottom],
  );

  const handleScroll = useCallback((event: UIEvent<HTMLDivElement>) => {
    followBottomRef.current = isNearBottom(event.currentTarget);
  }, []);

  useLayoutEffect(() => {
    if (forceBottomKey === lastForceBottomKeyRef.current) return;
    lastForceBottomKeyRef.current = forceBottomKey;
    scrollToBottom({ force: true });
    scheduleScrollToBottom({ force: true });
  }, [forceBottomKey, scheduleScrollToBottom, scrollToBottom]);

  useLayoutEffect(() => {
    scheduleScrollToBottom();
  }, [
    scheduleScrollToBottom,
    state.approvalOrder,
    state.approvals,
    state.artifacts,
    state.messages,
    state.statusLines,
    state.toolCalls,
    state.toolOrder,
  ]);

  useEffect(() => {
    const content = contentRef.current;
    if (!content || typeof ResizeObserver === "undefined") return;

    let frame: number | undefined;
    const observer = new ResizeObserver(() => {
      if (!followBottomRef.current) return;
      if (frame !== undefined) cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        frame = undefined;
        scheduleScrollToBottom();
      });
    });
    observer.observe(content);

    return () => {
      if (frame !== undefined) cancelAnimationFrame(frame);
      observer.disconnect();
    };
  }, [scheduleScrollToBottom]);

  useEffect(() => {
    return () => {
      if (scrollFrameRef.current !== undefined) {
        cancelAnimationFrame(scrollFrameRef.current);
      }
    };
  }, []);

  return (
    <div
      className="min-h-0 flex-1 overflow-y-auto px-3 py-4 sm:px-5"
      onScroll={handleScroll}
      ref={containerRef}
    >
      <div className="flex min-h-full flex-col gap-4" ref={contentRef}>
        {state.messages.length === 0 && state.toolOrder.length === 0 ? (
          <div className="m-auto max-w-xl border border-current/15 bg-midground/5 px-6 py-5 text-center">
            <h2 className="mb-2 font-display text-lg uppercase tracking-[0.12em] text-midground">
              Hermes GUI Chat beta
            </h2>
            <p className="text-sm text-text-secondary">
              Structured chat over /api/ws. Terminal Chat remains available at /chat.
            </p>
          </div>
        ) : null}

        {state.messages.map((message) => (
          <MessageBubble
            artifacts={message.artifactIds.map((id) => state.artifacts[id]).filter(Boolean)}
            key={message.id}
            message={message}
          />
        ))}

        {state.toolOrder.length > 0 ? (
          <div className="space-y-3">
            {state.toolOrder.map((id) => {
              const tool = state.toolCalls[id];
              if (!tool) return null;
              return (
                <ToolCallCard
                  artifacts={tool.artifactIds.map((artifactId) => state.artifacts[artifactId]).filter(Boolean)}
                  key={id}
                  tool={tool}
                />
              );
            })}
          </div>
        ) : null}

        {state.approvalOrder.map((id) => {
          const approval = state.approvals[id];
          if (!approval) return null;
          return (
            <ApprovalCard
              approval={approval}
              disabled={disabled}
              key={id}
              onRespond={(approved) => onApprovalRespond(id, approved)}
            />
          );
        })}

        {state.statusLines.length > 0 ? (
          <div className="space-y-1 text-xs text-text-tertiary">
            {state.statusLines.slice(-3).map((line, index) => (
              <div key={`${index}-${line}`} className="truncate">
                {line}
              </div>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}
