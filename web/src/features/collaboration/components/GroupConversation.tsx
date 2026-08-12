import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { AlertCircle, CheckCircle2, CircleStop, Clock3, FileText, LoaderCircle, ShieldAlert, UsersRound, XCircle } from "lucide-react";
import { useEffect, useMemo, useRef } from "react";
import { Markdown } from "@/components/Markdown";
import type { CollaborationApprovalChoice, CollaborationEmployeeIdentity, CollaborationState, CollaborationTarget } from "../types";
import { isTerminalTarget } from "../reducer";

interface GroupConversationProps {
  employees: CollaborationEmployeeIdentity[];
  onApproval(approvalId: string, choice: CollaborationApprovalChoice): void;
  onStop(targetId: string): void;
  state: CollaborationState;
}

export function GroupConversation({ employees, onApproval, onStop, state }: GroupConversationProps) {
  const endRef = useRef<HTMLDivElement>(null);
  const employeeName = useMemo(() => {
    const names = new Map(employees.map((employee) => [employee.employeeId, employee.name]));
    return (employeeId: string | null | undefined) => employeeId ? names.get(employeeId) ?? "Former employee" : "Hermes";
  }, [employees]);
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
  const targetsByEvent = useMemo(() => {
    const result = new Map<string, CollaborationTarget[]>();
    for (const target of Object.values(state.targetsById)) {
      const eventId = state.turnsById[target.turn_id]?.event_id;
      if (!eventId) continue;
      result.set(eventId, [...(result.get(eventId) ?? []), target]);
    }
    return result;
  }, [state.targetsById, state.turnsById]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [events.length, state.executionsById]);

  if (state.loading) {
    return <div className="flex flex-1 items-center justify-center gap-2 text-xs text-[#777c84]"><Spinner /> Loading group…</div>;
  }
  if (state.error && events.length === 0) {
    return <div className="flex flex-1 items-center justify-center gap-2 p-6 text-sm text-[#b42318]"><AlertCircle /> {state.error}</div>;
  }
  if (events.filter((event) => event.event_kind.startsWith("message.") || event.event_kind === "task.completed" || event.event_kind === "collaboration.origin.card").length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center p-8 text-center">
        <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-[#eef3ff] text-[#3867ed]"><UsersRound /></span>
        <h2 className="mt-4 text-base font-semibold text-[#25282d]">Start the group conversation</h2>
        <p className="mt-1 max-w-md text-xs leading-5 text-[#969aa1]">Select one or more employees, choose @all, or post without a mention for background context only.</p>
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
            const title = typeof event.body.title === "string" ? event.body.title : "Internal collaboration";
            const text = typeof event.body.text === "string" ? event.body.text : "";
            const groupId = typeof event.body.group_id === "string" ? event.body.group_id : "";
            const Icon = completed ? CheckCircle2 : UsersRound;
            return (
              <article className={`rounded-2xl border p-4 ${completed ? "border-[#cfe7d7] bg-[#f4fbf6]" : "border-[#dce4f7] bg-[#f7f9ff]"}`} key={event.event_id}>
                <div className="flex items-start gap-3">
                  <span className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-xl ${completed ? "bg-[#def3e5] text-[#238148]" : "bg-[#e6edff] text-[#3867ed]"}`}><Icon className="h-4 w-4" /></span>
                  <div className="min-w-0 flex-1"><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-[#6f7fa8]">Internal collaboration {status}</p><h3 className="mt-1 text-sm font-semibold text-[#252f4a]">{title}</h3>{text ? <div className="mt-2 text-xs leading-5 text-[#53617f]"><Markdown content={text} /></div> : null}{groupId ? <a className="mt-3 inline-flex text-xs font-semibold text-[#3867ed] hover:text-[#2852c7]" href={`?group=${encodeURIComponent(groupId)}`}>Open group</a> : null}</div>
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
                  <div className="min-w-0"><p className="text-[10px] font-semibold uppercase tracking-[0.12em] text-[#4f8060]">Internal collaboration completed</p>{summary ? <div className="mt-2 text-sm leading-6 text-[#31563d]"><Markdown content={summary} /></div> : null}</div>
                </div>
              </article>
            );
          }
          if (!event.event_kind.startsWith("message.")) return null;
          const owner = event.actor_kind === "owner";
          const text = typeof event.body.text === "string" ? event.body.text : "";
          const attachments = attachmentsByEvent.get(event.event_id) ?? [];
          const targets = targetsByEvent.get(event.event_id) ?? [];
          const speaker = owner ? "You" : employeeName(event.actor_employee_id);
          return (
            <article className={owner ? "ml-auto max-w-[85%]" : "max-w-[92%]"} key={event.event_id}>
              <div className={`mb-1.5 flex items-center gap-2 text-[11px] ${owner ? "justify-end" : ""}`}>
                {!owner ? <SpeakerAvatar name={speaker} /> : null}
                <span className="font-semibold text-[#4b4f56]">{speaker}</span>
                <span className="text-[#a1a5ac]">{formatTime(event.created_at)}</span>
              </div>
              <div className={owner ? "rounded-2xl rounded-tr-sm bg-[#eef3ff] px-4 py-3 text-[#283f79]" : "pl-8 text-[#25282d]"}>
                <Markdown content={text} />
                {attachments.length > 0 ? (
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {attachments.map((attachment) => <AttachmentChip key={attachment.attachment_id} name={attachment.filename} size={attachment.size_bytes} />)}
                  </div>
                ) : null}
              </div>
              {targets.length > 0 ? (
                <div className={`${owner ? "mt-3" : "ml-8 mt-3"} grid gap-2 sm:grid-cols-2`}>
                  {targets.map((target) => (
                    <TargetCard employeeName={employeeName} key={target.target_id} onStop={onStop} state={state} target={target} />
                  ))}
                </div>
              ) : null}
              {Object.values(state.approvalsById)
                .filter((approval) => targets.some((target) => target.target_id === approval.target_id))
                .map((approval) => (
                  <div className={`${owner ? "mt-2" : "ml-8 mt-2"} rounded-xl border border-[#f1d6a8] bg-[#fffaf0] p-3`} key={approval.approval_id}>
                    <div className="flex gap-2"><ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-[#a15c00]" /><div><p className="text-xs font-semibold text-[#704100]">{approval.request?.summary || "Approval required"}</p>{approval.request?.tool_name ? <p className="mt-1 text-[10px] text-[#956300]">Tool: {approval.request.tool_name}</p> : null}</div></div>
                    {approval.status === "pending" ? (
                      <div className="mt-3 flex flex-wrap gap-1.5">
                        <ApprovalButton label="Allow once" onClick={() => onApproval(approval.approval_id, "once")} />
                        <ApprovalButton label="Allow session" onClick={() => onApproval(approval.approval_id, "session")} />
                        {approval.request?.allow_permanent ? <ApprovalButton label="Always allow" onClick={() => onApproval(approval.approval_id, "always")} /> : null}
                        <ApprovalButton destructive label="Deny" onClick={() => onApproval(approval.approval_id, "deny")} />
                      </div>
                    ) : <p className="mt-2 text-[10px] font-medium uppercase tracking-wide text-[#956300]">{approval.status}</p>}
                  </div>
                ))}
            </article>
          );
        })}
        <div ref={endRef} />
      </div>
    </div>
  );
}

function TargetCard({ employeeName, onStop, state, target }: { employeeName(employeeId: string): string; onStop(targetId: string): void; state: CollaborationState; target: CollaborationTarget }) {
  const streamed = state.executionsById[target.execution_id] ?? "";
  const finalText = targetResultText(target);
  const status = targetStatus(target.status);
  const Icon = status.icon;
  return (
    <div className="rounded-xl border border-[#e4e6ea] bg-[#fafbfc] p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2"><SpeakerAvatar name={employeeName(target.employee_id)} small /><span className="truncate text-[11px] font-semibold">{employeeName(target.employee_id)}</span></div>
        <span className={`inline-flex items-center gap-1 text-[9px] font-semibold uppercase tracking-wide ${status.className}`}><Icon className={`h-3 w-3 ${target.status === "running" ? "animate-spin" : ""}`} />{target.status.replace("_", " ")}</span>
      </div>
      {(streamed || finalText) ? <div className="mt-2 max-h-40 overflow-auto text-xs leading-5 text-[#44484f]"><Markdown content={streamed || finalText} /></div> : null}
      {target.error ? <p className="mt-2 text-[10px] text-[#b42318]">{target.error}</p> : null}
      {!isTerminalTarget(target.status) ? <button className="mt-2 inline-flex items-center gap-1 text-[10px] font-medium text-[#777c84] hover:text-[#b42318]" onClick={() => onStop(target.target_id)} type="button"><CircleStop className="h-3 w-3" /> Stop this employee</button> : null}
    </div>
  );
}

function targetResultText(target: CollaborationTarget): string {
  if (typeof target.result?.text === "string") return target.result.text;
  if (typeof target.result?.content === "string") return target.result.content;
  if (typeof target.result?.message === "string") return target.result.message;
  return "";
}

function SpeakerAvatar({ name, small = false }: { name: string; small?: boolean }) {
  return <span className={`inline-flex shrink-0 items-center justify-center rounded-full bg-[#e6ebf9] font-semibold text-[#4665bb] ${small ? "h-5 w-5 text-[9px]" : "h-6 w-6 text-[10px]"}`}>{name.trim().charAt(0).toUpperCase() || "E"}</span>;
}

function AttachmentChip({ name, size }: { name: string; size: number }) {
  return <span className="inline-flex items-center gap-1 rounded-full bg-white/80 px-2 py-1 text-[10px]"><FileText className="h-3 w-3" />{name}<span className="text-[#969aa1]">{formatSize(size)}</span></span>;
}

function ApprovalButton({ destructive = false, label, onClick }: { destructive?: boolean; label: string; onClick(): void }) {
  return <button className={`rounded-md border px-2 py-1 text-[10px] font-medium ${destructive ? "border-[#efc7c2] text-[#b42318] hover:bg-[#fff1ef]" : "border-[#e7c987] text-[#815100] hover:bg-[#fff3d7]"}`} onClick={onClick} type="button">{label}</button>;
}

function targetStatus(status: CollaborationTarget["status"]): { className: string; icon: typeof Clock3 } {
  if (status === "completed") return { className: "text-[#238148]", icon: CheckCircle2 };
  if (status === "running") return { className: "text-[#3867ed]", icon: LoaderCircle };
  if (status === "queued" || status === "waiting_approval") return { className: "text-[#a15c00]", icon: Clock3 };
  if (status === "cancelled") return { className: "text-[#777c84]", icon: CircleStop };
  return { className: "text-[#b42318]", icon: XCircle };
}

function formatTime(value: number): string {
  return new Date(value * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
