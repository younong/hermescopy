import { Archive, ArchiveRestore, RefreshCw, UserRoundCog } from "lucide-react";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { guiChatTranslations, useI18n } from "@/i18n";
import { employeeDisplayName, employeeDisplayRole, type Employee } from "@/lib/api";
import { DEFAULT_HISTORY_PAGE_SIZE } from "@/lib/historyPagination";
import type { CollaborationApi } from "../api";
import type { CollaborationSubmitResponse } from "../protocol";
import { collaborationReducer, isTerminalTarget } from "../reducer";
import { defaultMentionSelection } from "../mentions";
import type { CollaborationGetOptions, CollaborationGroup, CollaborationSubmitMessage } from "../types";
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
  onUnarchive(groupId: string): Promise<CollaborationGroup>;
  onGroupChanged(): void;
}

export function GroupChatView({ api, connection, employees, groupId, onArchive, onUnarchive, onGroupChanged }: GroupChatViewProps) {
  const { t } = useI18n();
  const copy = guiChatTranslations(t).collaboration;
  const [state, dispatch] = useReducer(collaborationReducer, initialCollaborationState);
  const [memberManagerOpen, setMemberManagerOpen] = useState(false);
  const initialRef = useRef<AbortController | null>(null);
  const reconciliationRef = useRef<AbortController | null>(null);
  const historyRef = useRef<AbortController | null>(null);
  const generationRef = useRef(0);
  const pendingSubmitRef = useRef<CollaborationSubmitMessage | null>(null);
  const stateRef = useRef(state);
  useEffect(() => {
    stateRef.current = state;
  }, [state]);
  const connectionRef = useRef(connection);
  useEffect(() => {
    connectionRef.current = connection;
  }, [connection]);

  const activeOverlayOptions = useCallback((): CollaborationGetOptions => ({
    reconcile_approval_ids: Object.values(stateRef.current.approvalsById)
      .filter((approval) => approval.status === "pending")
      .map((approval) => approval.approval_id),
    reconcile_membership_ids: Object.values(stateRef.current.membershipsById)
      .filter((membership) => membership.leave_sequence === null)
      .map((membership) => membership.membership_id),
    reconcile_target_ids: Object.values(stateRef.current.targetsById)
      .filter((target) => !isTerminalTarget(target.status))
      .map((target) => target.target_id),
  }), []);

  const loadInitial = useCallback(async () => {
    initialRef.current?.abort();
    const controller = new AbortController();
    const generation = generationRef.current;
    initialRef.current = controller;
    dispatch({ type: "load.started", mode: "initial" });
    try {
      const snapshot = await api.getGroup(groupId, { limit: DEFAULT_HISTORY_PAGE_SIZE }, controller.signal);
      if (!controller.signal.aborted && generation === generationRef.current) {
        dispatch({ type: "snapshot", snapshot });
      }
    } catch (cause) {
      if (!controller.signal.aborted && generation === generationRef.current) {
        dispatch({ type: "load.failed", mode: "initial", message: errorMessage(cause) });
      }
    }
  }, [api, groupId]);

  const reconcile = useCallback(async () => {
    if (stateRef.current.group?.group_id !== groupId) return;
    reconciliationRef.current?.abort();
    const controller = new AbortController();
    const generation = generationRef.current;
    reconciliationRef.current = controller;
    dispatch({ type: "load.started", mode: "forward" });
    let after = stateRef.current.reconciledSequence;
    let through: number | undefined;
    try {
      while (!controller.signal.aborted) {
        const snapshot = await api.getGroup(groupId, {
          ...activeOverlayOptions(),
          after_sequence: after,
          limit: DEFAULT_HISTORY_PAGE_SIZE,
          ...(through === undefined ? {} : { through_sequence: through }),
        }, controller.signal);
        if (controller.signal.aborted || generation !== generationRef.current) return;
        dispatch({ type: "snapshot", snapshot });
        const page = snapshot.history_page;
        through ??= page?.through_sequence ?? snapshot.reconciliation?.last_sequence ?? snapshot.group.last_sequence;
        const next = page?.next_after_sequence ?? snapshot.reconciliation?.next_after_sequence ?? after;
        if (!(page?.has_more ?? false)) break;
        if (next <= after) throw new Error("Collaboration reconciliation cursor did not advance");
        after = next;
      }
    } catch (cause) {
      if (!controller.signal.aborted && generation === generationRef.current) {
        dispatch({ type: "load.failed", mode: "forward", message: errorMessage(cause) });
      }
    }
  }, [activeOverlayOptions, api, groupId]);

  const loadEarlier = useCallback(async () => {
    const before = stateRef.current.historyBeforeSequence;
    if (!before || !stateRef.current.historyHasMore || stateRef.current.historyLoading) return;
    historyRef.current?.abort();
    const controller = new AbortController();
    const generation = generationRef.current;
    historyRef.current = controller;
    dispatch({ type: "load.started", mode: "backward" });
    try {
      const snapshot = await api.getGroup(groupId, {
        before_sequence: before,
        limit: DEFAULT_HISTORY_PAGE_SIZE,
      }, controller.signal);
      if (!controller.signal.aborted && generation === generationRef.current) {
        dispatch({ type: "snapshot", snapshot });
      }
    } catch (cause) {
      if (!controller.signal.aborted && generation === generationRef.current) {
        dispatch({ type: "load.failed", mode: "backward", message: errorMessage(cause) });
      }
    }
  }, [api, groupId]);

  const applySubmitResult = useCallback((result: CollaborationSubmitResponse) => {
    dispatch({ type: "event", event: { type: "collaboration.event.appended", payload: result.event } });
    for (const target of result.turn?.targets ?? []) {
      dispatch({ type: "event", event: { type: "collaboration.target.changed", payload: { group_id: groupId, ...target } } });
    }
  }, [groupId]);

  useEffect(() => {
    generationRef.current += 1;
    dispatch({ type: "clear" });
    void loadInitial();
    return () => {
      generationRef.current += 1;
      initialRef.current?.abort();
      reconciliationRef.current?.abort();
      historyRef.current?.abort();
    };
  }, [groupId, loadInitial]);

  const previousConnectionRef = useRef(connection);
  useEffect(() => {
    const previousConnection = previousConnectionRef.current;
    previousConnectionRef.current = connection;
    dispatch({ type: "connection", state: connection });
    if (connection === "open" && previousConnection !== "open" && stateRef.current.group?.group_id === groupId) {
      void (async () => {
        await reconcile();
        const pending = pendingSubmitRef.current;
        if (!pending) return;
        try {
          const result = await api.submitMessage(pending);
          pendingSubmitRef.current = null;
          applySubmitResult(result);
        } catch {
          // Preserve the same idempotency key for the next reconnect.
        }
      })();
    }
  }, [api, applySubmitResult, connection, groupId, reconcile]);

  useEffect(() => api.onEvent((event) => {
    dispatch({ type: "event", event });
    if (event.type === "collaboration.group.changed") onGroupChanged();
  }), [api, onGroupChanged]);

  const identities: CollaborationEmployeeIdentity[] = useMemo(
    () => employees.map((employee) => ({
      employeeId: employee.employee_id,
      available: employee.lifecycle_status === "active" && employee.collaboration_policy.may_participate,
      avatarUrl: employee.avatar_url ?? undefined,
      name: employeeDisplayName(employee, copy.builtinAssistant, copy.unnamedEmployee),
      role: employeeDisplayRole(employee, copy.builtinDescription),
    })),
    [copy.builtinAssistant, copy.builtinDescription, copy.unnamedEmployee, employees],
  );
  const employeeName = useCallback((employeeId: string) =>
    identities.find((employee) => employee.employeeId === employeeId)?.name ?? copy.formerEmployee, [copy.formerEmployee, identities]);
  const memberships = useMemo(() => Object.values(state.membershipsById), [state.membershipsById]);
  const defaultSelection = useMemo(
    () => defaultMentionSelection(
      { mentionAll: false, membershipIds: [] },
      memberships.filter((member) => member.leave_sequence === null),
      Object.values(state.eventsBySequence),
    ),
    [memberships, state.eventsBySequence],
  );

  const submit = async ({ attachments, selection, text }: GroupComposerSubmit) => {
    const message: CollaborationSubmitMessage = {
      attachment_ids: attachments.map((attachment) => attachment.attachment_id),
      client_idempotency_key: createIdempotencyKey(),
      group_id: groupId,
      mention_all: selection.mentionAll,
      mentioned_membership_ids: selection.membershipIds,
      text,
    };
    try {
      const result = await api.submitMessage(message);
      pendingSubmitRef.current = null;
      applySubmitResult(result);
    } catch (cause) {
      if (connectionRef.current !== "open") pendingSubmitRef.current = message;
      throw cause;
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
        <button aria-label={copy.refreshGroup} className="gui-chat-icon-button" onClick={() => void reconcile()} type="button"><RefreshCw className={state.reconciling ? "animate-spin" : ""} /></button>
        {state.group?.status === "active" ? (
          <>
            <button aria-label={copy.manageMembers} className="gui-chat-icon-button" onClick={() => setMemberManagerOpen(true)} type="button"><UserRoundCog /></button>
            <button aria-label={copy.archiveGroup} className="gui-chat-icon-button" onClick={() => void onArchive(groupId)} type="button"><Archive /></button>
          </>
        ) : (
          <button
            aria-label={copy.unarchiveGroup}
            className="gui-chat-icon-button"
            onClick={() => void onUnarchive(groupId).then((group) => {
              dispatch({
                type: "event",
                event: { type: "collaboration.group.changed", payload: group },
              });
            })}
            type="button"
          >
            <ArchiveRestore />
          </button>
        )}
      </div>
      {state.error ? <div className="gui-chat-notice gui-chat-notice-error">{state.error}</div> : null}
      <GroupConversation employees={identities} onLoadEarlier={loadEarlier} state={state} />
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

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : String(cause);
}

function createIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return `web-${crypto.randomUUID()}`;
  return `web-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}
