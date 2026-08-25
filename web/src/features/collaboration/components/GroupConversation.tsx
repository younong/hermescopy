import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { useVirtualizer } from "@tanstack/react-virtual";
import { AlertCircle, CheckCircle2, FileText, UsersRound } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { UIEvent } from "react";
import { Markdown } from "@/components/Markdown";
import { useLoadEarlierOnScroll } from "@/hooks/useLoadEarlierOnScroll";
import { guiChatTranslations, useI18n } from "@/i18n";
import { withHermesAssetAuth } from "@/lib/api";
import { mentionLabel } from "../mentions";
import type { CollaborationEmployeeIdentity, CollaborationEvent, CollaborationState } from "../types";

const BOTTOM_THRESHOLD_PX = 64;
const OVERSCAN_ROWS = 10;

type ConversationRow =
  | { id: "history-control"; kind: "history" }
  | { event: CollaborationEvent; id: string; kind: "event" };

interface GroupConversationProps {
  employees: CollaborationEmployeeIdentity[];
  onLoadEarlier?: () => void | Promise<void>;
  state: CollaborationState;
}

export function GroupConversation({ employees, onLoadEarlier, state }: GroupConversationProps) {
  const { t } = useI18n();
  const translations = guiChatTranslations(t);
  const copy = translations.collaboration;
  const historyCopy = translations.messages;
  const containerRef = useRef<HTMLDivElement | null>(null);
  const followBottomRef = useRef(true);
  const anchorRef = useRef<{ id: string; offset: number } | null>(null);
  const initializedGroupRef = useRef<string | undefined>(undefined);
  const employeeIdentity = useMemo(() => {
    const identities = new Map(employees.map((employee) => [employee.employeeId, employee]));
    return (employeeId: string | null | undefined): CollaborationEmployeeIdentity =>
      employeeId
        ? identities.get(employeeId) ?? { available: false, employeeId, name: copy.formerEmployee }
        : { available: false, employeeId: "", name: "Hermes" };
  }, [copy.formerEmployee, employees]);
  const employeeName = useMemo(
    () => (employeeId: string | null | undefined) => employeeIdentity(employeeId).name,
    [employeeIdentity],
  );
  const events = useMemo(
    () => Object.values(state.eventsBySequence)
      .filter(isVisibleEvent)
      .sort((a, b) => a.sequence - b.sequence),
    [state.eventsBySequence],
  );
  const attachmentsByEvent = useMemo(() => {
    const result = new Map<string, typeof state.attachmentsById[string][]>();
    for (const attachment of Object.values(state.attachmentsById)) {
      if (!attachment.event_id) continue;
      result.set(attachment.event_id, [...(result.get(attachment.event_id) ?? []), attachment]);
    }
    return result;
  }, [state.attachmentsById]);
  const rows = useMemo<ConversationRow[]>(() => {
    const result: ConversationRow[] = [];
    if (state.historyHasMore || state.historyLoading || state.historyError) {
      result.push({ id: "history-control", kind: "history" });
    }
    for (const event of events) {
      result.push({ event, id: `event:${event.event_id}`, kind: "event" });
    }
    return result;
  }, [events, state.historyError, state.historyHasMore, state.historyLoading]);

  const virtualizer = useVirtualizer({
    count: rows.length,
    estimateSize: (index) => rows[index]?.kind === "history" ? 52 : 140,
    getItemKey: (index) => rows[index]?.id ?? index,
    getScrollElement: () => containerRef.current,
    initialRect: { height: 600, width: 800 },
    overscan: OVERSCAN_ROWS,
  });
  const totalSize = virtualizer.getTotalSize();
  const virtualItems = virtualizer.getVirtualItems();
  const renderedItems = virtualItems.length > 0
    ? virtualItems
    : rows.map((_, index) => ({ end: 0, index, key: rows[index]?.id ?? index, lane: 0, size: 0, start: 0 }));

  const captureAnchor = useCallback(() => {
    const container = containerRef.current;
    if (!container) return;
    const firstVisible = virtualizer.getVirtualItems().find((item) => {
      const row = rows[item.index];
      return row?.kind === "event" && item.end > container.scrollTop;
    });
    const row = firstVisible ? rows[firstVisible.index] : undefined;
    if (firstVisible && row) {
      anchorRef.current = { id: row.id, offset: firstVisible.start - container.scrollTop };
      followBottomRef.current = false;
    }
  }, [rows, virtualizer]);

  const { checkTop, handleScroll: handleHistoryScroll, retry, syncScrollPosition } = useLoadEarlierOnScroll({
    autoEnabled: !state.historyError,
    canLoad: state.historyHasMore && state.historyBeforeSequence !== undefined,
    loading: state.historyLoading,
    onBeforeLoad: captureAnchor,
    onLoadEarlier,
    resetKey: state.group?.group_id,
  });

  const scrollToBottom = useCallback((force = false) => {
    if (force) followBottomRef.current = true;
    if (!followBottomRef.current || rows.length === 0) return;
    virtualizer.scrollToIndex(rows.length - 1, { align: "end" });
    const element = containerRef.current;
    if (element) syncScrollPosition(element.scrollTop);
  }, [rows.length, syncScrollPosition, virtualizer]);

  const handleScroll = useCallback((event: UIEvent<HTMLDivElement>) => {
    const element = event.currentTarget;
    followBottomRef.current = element.scrollHeight - element.scrollTop - element.clientHeight <= BOTTOM_THRESHOLD_PX;
    handleHistoryScroll(event);
    checkTop(element.scrollTop);
  }, [checkTop, handleHistoryScroll]);

  useLayoutEffect(() => {
    const groupId = state.group?.group_id;
    if (!groupId || state.loading || initializedGroupRef.current === groupId) return;
    initializedGroupRef.current = groupId;
    scrollToBottom(true);
  }, [scrollToBottom, state.group?.group_id, state.loading]);

  useLayoutEffect(() => {
    const element = containerRef.current;
    if (!element || state.loading || state.historyLoading || !state.historyHasMore || state.historyBeforeSequence === undefined) return;
    if (element.scrollTop <= 200) checkTop(element.scrollTop);
  }, [checkTop, rows.length, state.historyBeforeSequence, state.historyHasMore, state.historyLoading, state.loading, totalSize]);

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
      return;
    }
    if (followBottomRef.current) scrollToBottom();
    else if (!state.historyLoading && state.historyHasMore && state.historyBeforeSequence !== undefined) {
      const element = containerRef.current;
      if (element && !anchorRef.current && element.scrollTop <= 200) checkTop(element.scrollTop);
    }
  }, [checkTop, rows, scrollToBottom, state.historyBeforeSequence, state.historyHasMore, state.historyLoading, syncScrollPosition, totalSize, virtualizer]);

  if (state.loading) {
    return <div className="flex flex-1 items-center justify-center gap-2 text-xs text-[#777c84]"><Spinner /> {copy.loadingGroup}</div>;
  }
  if (state.error && events.length === 0) {
    return <div className="flex flex-1 items-center justify-center gap-2 p-6 text-sm text-[#b42318]"><AlertCircle /> {state.error}</div>;
  }
  if (events.length === 0 && !state.historyHasMore && !state.historyLoading && !state.historyError) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center p-8 text-center">
        <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-[#eef3ff] text-[#3867ed]"><UsersRound /></span>
        <h2 className="mt-4 text-base font-semibold text-[#25282d]">{copy.startConversation}</h2>
        <p className="mt-1 max-w-md text-xs leading-5 text-[#969aa1]">{copy.startConversationHint}</p>
      </div>
    );
  }

  return (
    <div aria-busy={state.historyLoading} className="min-h-0 flex-1 overflow-y-auto px-4 py-5" onScroll={handleScroll} ref={containerRef}>
      <div className="relative mx-auto w-full max-w-3xl" style={{ height: `${totalSize}px` }}>
        {renderedItems.map((item) => {
          const row = rows[item.index];
          if (!row) return null;
          return (
            <div
              className="absolute left-0 top-0 w-full pb-5"
              data-index={item.index}
              key={row.id}
              ref={virtualizer.measureElement}
              style={{ transform: `translateY(${item.start}px)` }}
            >
              {row.kind === "history" ? (
                <div className="flex min-h-8 flex-col items-center justify-center gap-1 text-xs text-[#777c84]">
                  {state.historyError ? (
                    <>
                      <span role="alert">{state.historyError}</span>
                      <button className="rounded border border-current/20 px-3 py-1 hover:bg-black/5" disabled={state.historyLoading} onClick={retry} type="button">{historyCopy.retryEarlier}</button>
                    </>
                  ) : state.historyLoading ? (
                    <span aria-live="polite" role="status">{historyCopy.loadingEarlier}</span>
                  ) : (
                    <button className="rounded border border-current/20 px-3 py-1 hover:bg-black/5" onClick={retry} type="button">{historyCopy.scrollEarlier}</button>
                  )}
                </div>
              ) : (
                <EventCard
                  attachments={attachmentsByEvent.get(row.event.event_id) ?? []}
                  copy={copy}
                  employeeIdentity={employeeIdentity}
                  employeeName={employeeName}
                  event={row.event}
                  membershipsById={state.membershipsById}
                />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

function EventCard({ attachments, copy, employeeIdentity, employeeName, event, membershipsById }: {
  attachments: CollaborationState["attachmentsById"][string][];
  copy: ReturnType<typeof guiChatTranslations>["collaboration"];
  employeeIdentity(employeeId: string | null | undefined): CollaborationEmployeeIdentity;
  employeeName(employeeId: string | null | undefined): string;
  event: CollaborationEvent;
  membershipsById: CollaborationState["membershipsById"];
}) {
  const round = discussionRound(event.body, copy.discussionRound);
  if (event.event_kind === "discussion.round.started" && round) {
    return <div className="flex items-center gap-3 text-[10px] font-medium text-[#969aa1]"><span className="h-px flex-1 bg-[#e5e7eb]" /><span>{round}</span><span className="h-px flex-1 bg-[#e5e7eb]" /></div>;
  }
  if (event.event_kind === "collaboration.origin.card") {
    const status = event.body.status === "completed" ? "completed" : "created";
    const completed = status === "completed";
    const title = typeof event.body.title === "string" ? event.body.title : copy.internalCollaboration;
    const text = typeof event.body.text === "string" ? event.body.text : "";
    const groupId = typeof event.body.group_id === "string" ? event.body.group_id : "";
    const Icon = completed ? CheckCircle2 : UsersRound;
    return <article className={`rounded-2xl border p-4 ${completed ? "border-[#cfe7d7] bg-[#f4fbf6]" : "border-[#dce4f7] bg-[#f7f9ff]"}`}><div className="flex items-start gap-3"><span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-xl ${completed ? "bg-[#def3e5] text-[#238148]" : "bg-[#e6edff] text-[#3867ed]"}`}><Icon className="h-4 w-4" /></span><div className="min-w-0 flex-1"><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-[#6f7fa8]">{copy.internalCollaboration} {copy.status[status]}</p><h3 className="mt-1 text-sm font-semibold text-[#252f4a]">{title}</h3>{text ? <div className="mt-2 text-xs leading-5 text-[#53617f]"><Markdown content={text} /></div> : null}{groupId ? <a className="mt-3 inline-flex text-xs font-semibold text-[#3867ed] hover:text-[#2852c7]" href={`?group=${encodeURIComponent(groupId)}`}>{copy.openGroup}</a> : null}</div></div></article>;
  }
  if (event.event_kind === "task.completed") {
    const summary = typeof event.body.summary === "string" ? event.body.summary : "";
    return <article className="rounded-2xl border border-[#cfe7d7] bg-[#f4fbf6] p-4"><div className="flex items-start gap-3"><span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-[#def3e5] text-[#238148]"><CheckCircle2 className="h-4 w-4" /></span><div className="min-w-0"><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-[#4f8060]">{copy.internalCollaboration} {copy.status.completed}</p>{summary ? <div className="mt-2 text-sm leading-6 text-[#31563d]"><Markdown content={summary} /></div> : null}</div></div></article>;
  }
  const owner = event.actor_kind === "owner";
  const text = typeof event.body.text === "string" ? event.body.text : "";
  const mentions = owner ? mentionLabel({ mentionAll: event.body.mention_all === true, membershipIds: event.body.mentions ?? [] }, membershipsById, employeeName, { mentionAll: copy.mentionAll }) : "";
  const speaker = owner ? copy.you : employeeName(event.actor_employee_id);
  return <article className={owner ? "ml-auto max-w-[85%]" : "max-w-[92%]"}><div className={`mb-1.5 flex items-center gap-2 text-[11px] ${owner ? "justify-end" : ""}`}>{!owner ? <SpeakerAvatar employee={employeeIdentity(event.actor_employee_id)} /> : null}<span className="font-semibold text-[#4b4f56]">{speaker}</span><span className="text-[#a1a5ac]">{formatTime(event.created_at)}</span></div><div className={owner ? "rounded-2xl rounded-tr-sm bg-[#eef3ff] px-4 py-3 text-[#283f79]" : "pl-8 text-[#25282d]"}>{mentions ? <p className="mb-1 text-xs font-semibold text-[#4d73e6]">{mentions}</p> : null}{round ? <p className="mb-1 text-[10px] font-medium text-[#6f7fa8]">{round}</p> : null}<Markdown content={text} />{attachments.length > 0 ? <div className="mt-2 flex flex-wrap gap-1.5">{attachments.map((attachment) => <AttachmentChip key={attachment.attachment_id} name={attachment.filename} size={attachment.size_bytes} />)}</div> : null}</div></article>;
}

function isVisibleEvent(event: CollaborationEvent): boolean {
  return event.event_kind.startsWith("message.")
    || event.event_kind === "discussion.round.started"
    || event.event_kind === "task.completed"
    || event.event_kind === "collaboration.origin.card";
}

function discussionRound(body: { discussion_round?: number; total_rounds?: number }, template: string): string {
  if (!body.discussion_round || !body.total_rounds) return "";
  return template.replace("{round}", String(body.discussion_round)).replace("{total}", String(body.total_rounds));
}

function SpeakerAvatar({ employee }: { employee: CollaborationEmployeeIdentity }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [employee.avatarUrl]);
  const classes = "h-6 w-6 text-[10px]";
  return employee.avatarUrl && !failed
    ? <img alt="" className={`shrink-0 rounded-full object-cover ${classes}`} onError={() => setFailed(true)} src={withHermesAssetAuth(employee.avatarUrl)} />
    : <span className={`inline-flex shrink-0 items-center justify-center rounded-full bg-[#e6ebf9] font-semibold text-[#4665bb] ${classes}`}>{employee.name.trim().charAt(0).toUpperCase() || "E"}</span>;
}

function AttachmentChip({ name, size }: { name: string; size: number }) {
  return <span className="inline-flex items-center gap-1 rounded-full bg-white/80 px-2 py-1 text-[10px]"><FileText className="h-3 w-3" />{name}<span className="text-[#969aa1]">{formatSize(size)}</span></span>;
}

function formatTime(value: number): string {
  return new Date(value * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
