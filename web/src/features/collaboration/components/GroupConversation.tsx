import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { AlertCircle, CheckCircle2, FileText, UsersRound } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Markdown } from "@/components/Markdown";
import { guiChatTranslations, useI18n } from "@/i18n";
import { withHermesAssetAuth } from "@/lib/api";
import { mentionLabel } from "../mentions";
import type { CollaborationEmployeeIdentity, CollaborationState } from "../types";

interface GroupConversationProps {
  employees: CollaborationEmployeeIdentity[];
  state: CollaborationState;
}

export function GroupConversation({ employees, state }: GroupConversationProps) {
  const { t } = useI18n();
  const copy = guiChatTranslations(t).collaboration;
  const endRef = useRef<HTMLDivElement>(null);
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
    () => Object.values(state.eventsBySequence).sort((a, b) => a.sequence - b.sequence),
    [state.eventsBySequence],
  );
  const attachmentsByEvent = useMemo(() => {
    const result = new Map<string, typeof state.attachmentsById[string][]>();
    for (const attachment of Object.values(state.attachmentsById)) {
      if (!attachment.event_id) continue;
      result.set(attachment.event_id, [...(result.get(attachment.event_id) ?? []), attachment]);
    }
    return result;
  }, [state]);
  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [events.length]);

  if (state.loading) {
    return <div className="flex flex-1 items-center justify-center gap-2 text-xs text-[#777c84]"><Spinner /> {copy.loadingGroup}</div>;
  }
  if (state.error && events.length === 0) {
    return <div className="flex flex-1 items-center justify-center gap-2 p-6 text-sm text-[#b42318]"><AlertCircle /> {state.error}</div>;
  }
  if (events.filter((event) => event.event_kind.startsWith("message.") || event.event_kind === "task.completed" || event.event_kind === "collaboration.origin.card").length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center p-8 text-center">
        <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-[#eef3ff] text-[#3867ed]"><UsersRound /></span>
        <h2 className="mt-4 text-base font-semibold text-[#25282d]">{copy.startConversation}</h2>
        <p className="mt-1 max-w-md text-xs leading-5 text-[#969aa1]">{copy.startConversationHint}</p>
      </div>
    );
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-4 py-5">
      <div className="mx-auto flex max-w-3xl flex-col gap-5">
        {events.map((event) => {
          if (event.event_kind === "collaboration.origin.card") {
            const status = event.body.status === "completed" ? "completed" : "created";
            const completed = status === "completed";
            const title = typeof event.body.title === "string" ? event.body.title : copy.internalCollaboration;
            const text = typeof event.body.text === "string" ? event.body.text : "";
            const groupId = typeof event.body.group_id === "string" ? event.body.group_id : "";
            const Icon = completed ? CheckCircle2 : UsersRound;
            return (
              <article className={`rounded-2xl border p-4 ${completed ? "border-[#cfe7d7] bg-[#f4fbf6]" : "border-[#dce4f7] bg-[#f7f9ff]"}`} key={event.event_id}>
                <div className="flex items-start gap-3">
                  <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-xl ${completed ? "bg-[#def3e5] text-[#238148]" : "bg-[#e6edff] text-[#3867ed]"}`}><Icon className="h-4 w-4" /></span>
                  <div className="min-w-0 flex-1"><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-[#6f7fa8]">{copy.internalCollaboration} {copy.status[status]}</p><h3 className="mt-1 text-sm font-semibold text-[#252f4a]">{title}</h3>{text ? <div className="mt-2 text-xs leading-5 text-[#53617f]"><Markdown content={text} /></div> : null}{groupId ? <a className="mt-3 inline-flex text-xs font-semibold text-[#3867ed] hover:text-[#2852c7]" href={`?group=${encodeURIComponent(groupId)}`}>{copy.openGroup}</a> : null}</div>
                </div>
              </article>
            );
          }
          if (event.event_kind === "task.completed") {
            const summary = typeof event.body.summary === "string" ? event.body.summary : "";
            return (
              <article className="rounded-2xl border border-[#cfe7d7] bg-[#f4fbf6] p-4" key={event.event_id}>
                <div className="flex items-start gap-3">
                  <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-xl bg-[#def3e5] text-[#238148]"><CheckCircle2 className="h-4 w-4" /></span>
                  <div className="min-w-0"><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-[#4f8060]">{copy.internalCollaboration} {copy.status.completed}</p>{summary ? <div className="mt-2 text-sm leading-6 text-[#31563d]"><Markdown content={summary} /></div> : null}</div>
                </div>
              </article>
            );
          }
          if (!event.event_kind.startsWith("message.")) return null;
          const owner = event.actor_kind === "owner";
          const text = typeof event.body.text === "string" ? event.body.text : "";
          const mentions = owner ? mentionLabel({
            mentionAll: event.body.mention_all === true,
            membershipIds: event.body.mentions ?? [],
          }, state.membershipsById, employeeName, { mentionAll: copy.mentionAll }) : "";
          const attachments = attachmentsByEvent.get(event.event_id) ?? [];
          const speaker = owner ? copy.you : employeeName(event.actor_employee_id);
          return (
            <article className={owner ? "ml-auto max-w-[85%]" : "max-w-[92%]"} key={event.event_id}>
              <div className={`mb-1.5 flex items-center gap-2 text-[11px] ${owner ? "justify-end" : ""}`}>
                {!owner ? <SpeakerAvatar employee={employeeIdentity(event.actor_employee_id)} /> : null}
                <span className="font-semibold text-[#4b4f56]">{speaker}</span>
                <span className="text-[#a1a5ac]">{formatTime(event.created_at)}</span>
              </div>
              <div className={owner ? "rounded-2xl rounded-tr-sm bg-[#eef3ff] px-4 py-3 text-[#283f79]" : "pl-8 text-[#25282d]"}>
                {mentions ? <p className="mb-1 text-xs font-semibold text-[#4d73e6]">{mentions}</p> : null}
                <Markdown content={text} />
                {attachments.length > 0 ? (
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {attachments.map((attachment) => <AttachmentChip key={attachment.attachment_id} name={attachment.filename} size={attachment.size_bytes} />)}
                  </div>
                ) : null}
              </div>
            </article>
          );
        })}
        <div ref={endRef} />
      </div>
    </div>
  );
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
