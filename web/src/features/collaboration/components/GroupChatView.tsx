import { Archive, RefreshCw, UserRoundCog } from "lucide-react";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { guiChatTranslations, useI18n } from "@/i18n";
import type { Employee } from "@/lib/api";
import type { CollaborationApi } from "../api";
import { collaborationReducer } from "../reducer";
import { defaultMentionSelection } from "../mentions";
import { initialCollaborationState, type CollaborationEmployeeIdentity } from "../types";
import { GroupComposer, type GroupComposerSubmit } from "./GroupComposer";
import { GroupConversation } from "./GroupConversation";
import { MemberManager } from "./MemberManager";

interface GroupChatViewProps {
  api: CollaborationApi;
  connection: typeof initialCollaborationState.connection;
  employees: Employee[];
  groupId: string;
  onArchive(groupId: string): Promise<void>;
  onGroupChanged(): void;
}

export function GroupChatView({ api, connection, employees, groupId, onArchive, onGroupChanged }: GroupChatViewProps) {
  const { t } = useI18n();
  const copy = guiChatTranslations(t).collaboration;
  const [state, dispatch] = useReducer(collaborationReducer, initialCollaborationState);
  const [memberManagerOpen, setMemberManagerOpen] = useState(false);
  const loadRef = useRef<AbortController | null>(null);
  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = state;
  }, [state]);

  const load = useCallback(async (incremental: boolean) => {
    loadRef.current?.abort();
    const controller = new AbortController();
    loadRef.current = controller;
    const after = incremental && stateRef.current.group?.group_id === groupId
      ? stateRef.current.lastSequence
      : undefined;
    if (incremental && after === undefined) return;
    dispatch({ type: "load.started", incremental: after !== undefined });
    try {
      const snapshot = await api.getGroup(groupId, after, controller.signal);
      if (!controller.signal.aborted) dispatch({ type: "snapshot", snapshot });
    } catch (cause) {
      if (!controller.signal.aborted) dispatch({ type: "error", message: cause instanceof Error ? cause.message : String(cause) });
    }
  }, [api, groupId]);

  useEffect(() => {
    dispatch({ type: "clear" });
    void load(false);
    return () => loadRef.current?.abort();
  }, [groupId, load]);

  const previousConnectionRef = useRef(connection);
  useEffect(() => {
    const previousConnection = previousConnectionRef.current;
    previousConnectionRef.current = connection;
    dispatch({ type: "connection", state: connection });
    if (
      connection === "open" &&
      previousConnection !== "open" &&
      stateRef.current.group?.group_id === groupId
    ) {
      void load(true);
    }
  }, [connection, groupId, load]);

  useEffect(() => api.onEvent((event) => {
    dispatch({ type: "event", event });
    if (event.type === "collaboration.group.changed") onGroupChanged();
  }), [api, onGroupChanged]);

  const identities: CollaborationEmployeeIdentity[] = useMemo(
    () => employees.map((employee) => ({
      employeeId: employee.employee_id,
      available: employee.lifecycle_status === "active" && employee.collaboration_policy.may_participate,
      avatarUrl: employee.avatar_url ?? undefined,
      name: employee.profile?.name || copy.unnamedEmployee,
      role: employee.profile?.role,
    })),
    [copy.unnamedEmployee, employees],
  );
  const employeeName = useCallback((employeeId: string) =>
    identities.find((employee) => employee.employeeId === employeeId)?.name ?? copy.formerEmployee, [copy.formerEmployee, identities]);
  const memberships = useMemo(
    () => Object.values(state.membershipsById),
    [state.membershipsById],
  );
  const defaultSelection = useMemo(
    () => defaultMentionSelection(
      { mentionAll: false, membershipIds: [] },
      memberships.filter((member) => member.leave_sequence === null),
      Object.values(state.eventsBySequence),
    ),
    [memberships, state.eventsBySequence],
  );

  const submit = async ({ attachments, selection, text }: GroupComposerSubmit) => {
    const result = await api.submitMessage({
      attachment_ids: attachments.map((attachment) => attachment.attachment_id),
      client_idempotency_key: createIdempotencyKey(),
      group_id: groupId,
      mention_all: selection.mentionAll,
      mentioned_membership_ids: selection.membershipIds,
      text,
    });
    dispatch({ type: "event", event: { type: "collaboration.event.appended", payload: result.event } });
    for (const target of result.turn?.targets ?? []) {
      dispatch({ type: "event", event: { type: "collaboration.target.changed", payload: { group_id: groupId, ...target } } });
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {memberManagerOpen ? (
        <MemberManager
          employees={employees}
          memberships={memberships}
          onClose={() => setMemberManagerOpen(false)}
          onSave={async (employeeIds) => {
            const snapshot = await api.updateMembers(groupId, employeeIds);
            dispatch({ type: "snapshot", snapshot });
            setMemberManagerOpen(false);
            onGroupChanged();
          }}
        />
      ) : null}
      <div className="flex h-10 shrink-0 items-center justify-end gap-1 border-b border-[#f0f1f3] px-3">
        <button aria-label={copy.refreshGroup} className="gui-chat-icon-button" onClick={() => void load(true)} type="button"><RefreshCw className={state.reconciling ? "animate-spin" : ""} /></button>
        {state.group?.status === "active" ? (
          <>
            <button aria-label={copy.manageMembers} className="gui-chat-icon-button" onClick={() => setMemberManagerOpen(true)} type="button"><UserRoundCog /></button>
            <button aria-label={copy.archiveGroup} className="gui-chat-icon-button" onClick={() => void onArchive(groupId)} type="button"><Archive /></button>
          </>
        ) : null}
      </div>
      {state.error ? <div className="gui-chat-notice gui-chat-notice-error">{state.error}</div> : null}
      <GroupConversation employees={identities} state={state} />
      <GroupComposer
        employeeName={employeeName}
        archived={state.group?.status === "archived"}
        defaultSelection={defaultSelection}
        disabled={connection !== "open" || state.loading}
        memberships={memberships}
        onSubmit={submit}
        onUpload={(file) => api.uploadAttachment(groupId, file).then((response) => response.attachment)}
      />
    </div>
  );
}

function createIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return `web-${crypto.randomUUID()}`;
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}
