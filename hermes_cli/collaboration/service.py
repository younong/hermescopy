"""Trusted Owner Worker service for internal collaboration groups."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict
import hashlib
import uuid
from typing import Any, Callable, Protocol

from hermes_cli.attachment_uploads import validate_upload
from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext
from hermes_cli.controlled_roots import RootKind
from hermes_state import SessionDB

from .agent_tools import CollaborationAgentContext
from .resolver import CollaborationEmployeeResolver, collaboration_member_policy
from .store import CollaborationStore


class SchedulerControl(Protocol):
    def wake(self) -> None: ...

    def interrupt(self, target_id: str) -> dict[str, Any]: ...

    def interrupt_session(self, hidden_session_id: str) -> bool: ...

    def respond_approval(self, approval_id: str, choice: str) -> dict[str, Any]: ...


class CollaborationService:
    """Validate browser identifiers and keep policy resolution server-authoritative."""

    def __init__(
        self,
        db: SessionDB,
        *,
        owner_key: str,
        resolver: CollaborationEmployeeResolver,
        emit: Callable[[str, dict[str, Any]], None],
        ensure_member_session: Callable[..., None],
        provision_member_session: Callable[..., None] | None = None,
        filesystem_context: AuthenticatedWorkspaceContext | None = None,
        deliver_web_origin: Callable[..., None] | None = None,
        worker_id: str | None = None,
        worker_generation: int | None = None,
        lease_version: int | None = None,
        recovery_generation: int | None = None,
    ) -> None:
        self.db = db
        self.store = CollaborationStore(db, owner_key=owner_key)
        self.owner_key = self.store.owner_key
        self.resolver = resolver
        self.emit = emit
        self.ensure_member_session = ensure_member_session
        self.provision_member_session = provision_member_session
        self.filesystem_context = filesystem_context
        self.deliver_web_origin = deliver_web_origin
        self.worker_id = str(worker_id or "") or None
        self.worker_generation = worker_generation
        self.lease_version = lease_version
        self.recovery_generation = recovery_generation
        self.scheduler: SchedulerControl | None = None

    def bind_scheduler(self, scheduler: SchedulerControl) -> None:
        if self.scheduler is not None:
            raise RuntimeError("collaboration scheduler is already bound")
        self.scheduler = scheduler

    def list_groups(self, *, include_archived: bool = False) -> dict[str, Any]:
        return {
            "groups": [
                self._public_group(asdict(group))
                for group in self.store.list_groups(include_archived=include_archived)
            ]
        }

    def get_group(
        self,
        group_id: str,
        *,
        after_sequence: int | None = None,
    ) -> dict[str, Any]:
        return self._public_snapshot(
            self.store.snapshot_payload(group_id, after_sequence=after_sequence)
        )

    def create_group(
        self,
        *,
        name: str,
        account_ids: Iterable[str] = (),
        client_idempotency_key: str,
    ) -> dict[str, Any]:
        resolved = self._resolve_accounts(account_ids)
        resolved = tuple(sorted(resolved, key=lambda item: item.member.account_id))
        policies = {item.member.account_id: item.employee_policy for item in resolved}

        def _provision(membership, member) -> None:
            policy = collaboration_member_policy(
                policies[member.account_id], membership.membership_id
            )
            if self.provision_member_session is not None:
                self.provision_member_session(membership, policy)

        group = self.store.create_group(
            name,
            members=[item.member for item in resolved],
            client_idempotency_key=client_idempotency_key,
            provision_member=_provision,
        )
        for membership in self.store.active_memberships(group.group_id):
            policy = collaboration_member_policy(
                policies[membership.account_id], membership.membership_id
            )
            self.ensure_member_session(
                membership=membership,
                employee_policy=policy,
            )
        payload = self.get_group(group.group_id)
        self.emit("collaboration.group.changed", payload["group"])
        return payload

    def archive_group(self, group_id: str) -> dict[str, Any]:
        group, live_session_ids = self.store.archive_group(group_id)
        scheduler = self.scheduler
        if scheduler is not None:
            for hidden_session_id in live_session_ids:
                try:
                    scheduler.interrupt_session(hidden_session_id)
                except Exception:
                    pass
            scheduler.wake()
        payload = self._public_group(asdict(group))
        self.emit("collaboration.group.changed", payload)
        return {"group": payload}

    def update_members(
        self,
        group_id: str,
        *,
        account_ids: Iterable[str],
    ) -> dict[str, Any]:
        requested = tuple(dict.fromkeys(self._account_id(value) for value in account_ids))
        current = {item.account_id: item for item in self.store.active_memberships(group_id)}
        requested_set = set(requested)
        resolved_policies: dict[str, dict[str, Any]] = {}
        for account_id in requested:
            membership = current.get(account_id)
            if membership is None:
                continue
            resolved = self.resolver.resolve_pinned(
                account_id=membership.account_id,
                profile_revision=membership.profile_revision,
                profile_fingerprint=membership.profile_fingerprint,
            )
            if not resolved.may_participate:
                raise RuntimeError("collaboration participation is revoked")
            resolved_policies[account_id] = resolved.employee_policy
        additions = {
            item.member.account_id: item
            for item in self._resolve_accounts(
                account_id for account_id in requested if account_id not in current
            )
        }
        resolved_policies.update(
            {account_id: item.employee_policy for account_id, item in additions.items()}
        )

        def _provision(membership, member) -> None:
            policy = collaboration_member_policy(
                resolved_policies[member.account_id], membership.membership_id
            )
            if self.provision_member_session is not None:
                self.provision_member_session(membership, policy)

        memberships = self.store.update_memberships(
            group_id,
            requested_account_ids=requested,
            additions={account_id: item.member for account_id, item in additions.items()},
            provision_member=_provision,
        )
        for membership in memberships:
            policy = additions.get(membership.account_id)
            if policy is not None:
                self.ensure_member_session(
                    membership=membership,
                    employee_policy=collaboration_member_policy(
                        policy.employee_policy, membership.membership_id
                    ),
                )
        payload = self.get_group(group_id)
        self.emit("collaboration.group.changed", payload["group"])
        return payload

    def attach(
        self,
        group_id: str,
        *,
        kind: str,
        filename: str,
        content_base64: str,
        media_type: str | None = None,
    ) -> dict[str, Any]:
        context = self.filesystem_context
        if not isinstance(context, AuthenticatedWorkspaceContext):
            raise RuntimeError("authenticated collaboration storage is unavailable")
        upload = validate_upload(
            kind=kind,
            filename=filename,
            content_base64=content_base64,
            media_type=media_type,
        )
        digest = hashlib.sha256(upload.data).hexdigest()
        storage_key = (
            f"collaboration/{group_id}/{uuid.uuid4().hex}/"
            f"{digest[:16]}-{upload.filename}"
        )
        context.roots.replace_bytes(
            RootKind.OWNER_WRITABLE,
            storage_key,
            upload.data,
            overwrite=False,
        )
        try:
            attachment = self.store.create_attachment(
                group_id,
                filename=upload.filename,
                media_type=upload.media_type,
                size_bytes=len(upload.data),
                storage_key=storage_key,
                content_sha256=digest,
            )
        except Exception:
            context.roots.remove(RootKind.OWNER_WRITABLE, storage_key)
            raise
        return {"attachment": attachment}

    def submit_message(
        self,
        group_id: str,
        *,
        text: str,
        mentioned_membership_ids: Iterable[str] = (),
        mention_all: bool = False,
        attachment_ids: Iterable[str] = (),
        client_idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        submitted = self.store.submit_owner_message(
            group_id,
            text=text,
            mentioned_membership_ids=mentioned_membership_ids,
            mention_all=mention_all,
            attachment_ids=attachment_ids,
            client_idempotency_key=client_idempotency_key,
        )
        event = asdict(submitted.event)
        self.emit("collaboration.event.appended", event)
        if submitted.turn is not None:
            for target in submitted.turn.targets:
                self.emit(
                    "collaboration.target.changed",
                    {"group_id": submitted.turn.group_id, **asdict(target)},
                )
            if self.scheduler is None:
                raise RuntimeError("collaboration scheduler is unavailable")
            self.scheduler.wake()
        return {
            "event": event,
            "turn": (
                {
                    "turn_id": submitted.turn.turn_id,
                    "group_id": submitted.turn.group_id,
                    "event_id": submitted.turn.event_id,
                    "snapshot_sequence": submitted.turn.snapshot_sequence,
                    "status": submitted.turn.status,
                    "targets": [asdict(target) for target in submitted.turn.targets],
                }
                if submitted.turn is not None
                else None
            ),
        }

    def source_agent_context(
        self,
        *,
        creator_account_id: str,
        source_kind: str,
        source_conversation_id: str,
        source_provider: str = "web",
        source_account_id: str | None = None,
        source_binding_id: str | None = None,
        source_thread_id: str = "",
        source_session_id: str | None = None,
        source_group_id: str | None = None,
        source_event_id: str | None = None,
        allowed_origin_attachment_ids: Iterable[str] = (),
    ) -> CollaborationAgentContext:
        """Build one trusted source context after resolving live authorization."""
        creator = self.resolver.resolve_current(self._account_id(creator_account_id))
        if not creator.may_participate:
            raise RuntimeError("collaboration participation is revoked")
        return CollaborationAgentContext(
            service=self,
            creator_account_id=creator.member.account_id,
            source_kind=str(source_kind),
            source_conversation_id=str(source_conversation_id),
            source_provider=str(source_provider),
            source_account_id=source_account_id,
            source_binding_id=source_binding_id,
            source_thread_id=str(source_thread_id or ""),
            source_session_id=source_session_id,
            source_group_id=source_group_id,
            source_event_id=source_event_id,
            source_depth=0,
            allowed_origin_attachment_ids=tuple(
                dict.fromkeys(
                    str(value or "").strip() for value in allowed_origin_attachment_ids
                )
            ),
            role="source",
            may_create_authorized=bool(creator.may_create_groups),
        )

    def create_internal_group(
        self,
        *,
        context: CollaborationAgentContext,
        title: str,
        brief: str,
        invitee_account_ids: Iterable[str],
        origin_attachment_ids: Iterable[str],
        first_round_target_account_ids: Iterable[str],
        idempotency_key: str,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        del tool_call_id
        self._require_bound_context(context, role="source")
        if int(context.source_depth) != 0 or context.source_task_id is not None:
            raise RuntimeError("nested collaboration group creation is unavailable")
        creator = self.resolver.resolve_current(context.creator_account_id)
        if not creator.may_participate:
            raise RuntimeError("collaboration participation is revoked")
        if not creator.may_create_groups:
            raise RuntimeError("collaboration group creation is not authorized")
        invitees = tuple(dict.fromkeys(self._account_id(value) for value in invitee_account_ids))
        if creator.invite_quota is not None and len(invitees) > int(creator.invite_quota):
            raise RuntimeError("collaboration invitation quota exceeded")
        targets = tuple(
            dict.fromkeys(self._account_id(value) for value in first_round_target_account_ids)
        )
        if not targets or not set(targets) <= set(invitees):
            raise RuntimeError("first-round targets must be invited employees")
        selected_attachments = tuple(
            dict.fromkeys(str(value or "").strip() for value in origin_attachment_ids)
        )
        if any(not value for value in selected_attachments):
            raise ValueError("origin attachment ID is required")
        if not set(selected_attachments) <= set(context.allowed_origin_attachment_ids):
            raise RuntimeError("origin attachment is not allowed")
        resolved = self._resolve_accounts(invitees)
        result, created = self.store.create_ai_task(
            title=title,
            brief=brief,
            creator=creator.member,
            members=[item.member for item in resolved],
            source_kind=context.source_kind,
            source_provider=context.source_provider,
            source_account_id=context.source_account_id,
            source_binding_id=context.source_binding_id,
            source_conversation_id=context.source_conversation_id,
            source_thread_id=context.source_thread_id,
            source_session_id=context.source_session_id,
            source_group_id=context.source_group_id,
            source_event_id=context.source_event_id,
            source_task_id=context.source_task_id,
            depth=1,
            allowed_attachment_ids=selected_attachments,
            idempotency_key=idempotency_key,
        )
        active = self.store.active_memberships(result["group_id"])
        policies = {
            creator.member.account_id: creator,
            **{item.member.account_id: item for item in resolved},
        }
        for membership in active:
            item = policies[membership.account_id]
            self.ensure_member_session(
                membership=membership,
                employee_policy=collaboration_member_policy(
                    item.employee_policy, membership.membership_id
                ),
            )
        if created:
            snapshot = self.get_group(result["group_id"])
            self.emit("collaboration.group.changed", snapshot["group"])
        if int(self.store.ai_task(result["task_id"])["round"]) == 0:
            self.dispatch_internal_group_round(
                context=CollaborationAgentContext(
                    service=self,
                    creator_account_id=context.creator_account_id,
                    source_kind=context.source_kind,
                    source_conversation_id=context.source_conversation_id,
                    source_provider=context.source_provider,
                    source_account_id=context.source_account_id,
                    source_binding_id=context.source_binding_id,
                    source_thread_id=context.source_thread_id,
                    source_session_id=context.source_session_id,
                    source_group_id=context.source_group_id,
                    source_event_id=context.source_event_id,
                    source_task_id=context.source_task_id,
                    source_depth=1,
                    allowed_origin_attachment_ids=tuple(result["allowed_attachment_ids"]),
                    task_id=result["task_id"],
                    role="coordinator",
                ),
                instruction=brief,
                target_account_ids=targets,
                attachment_ids=tuple(result["allowed_attachment_ids"]),
                idempotency_key=f"{idempotency_key}:round:1",
            )
        self._deliver_origin_card(result["task_id"], completion=False)
        current = self.store.ai_task(result["task_id"])
        return {
            "task_id": result["task_id"],
            "group_id": result["group_id"],
            "round": int(current["round"]),
            "status": str(current["status"]),
        }

    def dispatch_internal_group_round(
        self,
        *,
        context: CollaborationAgentContext,
        instruction: str,
        target_account_ids: Iterable[str],
        attachment_ids: Iterable[str],
        idempotency_key: str,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        del tool_call_id
        self._require_bound_context(context, role="coordinator")
        task = self.store.ai_task(str(context.task_id or ""))
        self._require_task_creator(task, context)
        self._recheck_creator_authorization(task)
        target_accounts = tuple(
            dict.fromkeys(self._account_id(value) for value in target_account_ids)
        )
        memberships = {
            item.account_id: item for item in self.store.active_memberships(task["group_id"])
        }
        if not target_accounts or not set(target_accounts) <= set(memberships):
            raise RuntimeError("collaboration target is not an active member")
        selected = tuple(dict.fromkeys(str(value or "").strip() for value in attachment_ids))
        if any(not value for value in selected) or not set(selected) <= set(task["allowed_attachment_ids"]):
            raise RuntimeError("collaboration attachment is not allowed for this task")
        for account_id in target_accounts:
            membership = memberships[account_id]
            resolved = self.resolver.resolve_pinned(
                account_id=membership.account_id,
                profile_revision=membership.profile_revision,
                profile_fingerprint=membership.profile_fingerprint,
            )
            if not resolved.may_participate:
                raise RuntimeError("collaboration participation is revoked")
        submitted, next_round, created = self.store.dispatch_ai_round(
            task["task_id"],
            instruction=instruction,
            target_account_ids=target_accounts,
            attachment_ids=selected,
            idempotency_key=idempotency_key,
        )
        if submitted.turn is None:
            raise RuntimeError("collaboration round has no targets")
        if created:
            event = asdict(submitted.event)
            self.emit("collaboration.event.appended", event)
            for target in submitted.turn.targets:
                self.emit(
                    "collaboration.target.changed",
                    {"group_id": submitted.turn.group_id, **asdict(target)},
                )
            if self.scheduler is None:
                raise RuntimeError("collaboration scheduler is unavailable")
            self.scheduler.wake()
        return {
            "task_id": task["task_id"],
            "group_id": task["group_id"],
            "turn_id": submitted.turn.turn_id,
            "round": next_round,
            "status": submitted.turn.status,
        }

    def finish_internal_group_task(
        self,
        *,
        context: CollaborationAgentContext,
        summary: str,
        idempotency_key: str,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        del tool_call_id
        self._require_bound_context(context, role="coordinator")
        task = self.store.ai_task(str(context.task_id or ""))
        self._require_task_creator(task, context)
        self._recheck_creator_authorization(task)
        result, changed = self.store.complete_ai_task(
            task["task_id"], summary=summary, idempotency_key=idempotency_key
        )
        if changed and result.get("event_id"):
            event = self.store.list_events_payload(
                result["group_id"],
                after_sequence=self.store.get_group(result["group_id"]).last_sequence - 1,
            )["events"][0]
            self.emit("collaboration.event.appended", event)
        self._deliver_origin_card(task["task_id"], completion=True)
        return result

    def _require_bound_context(self, context: CollaborationAgentContext, *, role: str) -> None:
        if not isinstance(context, CollaborationAgentContext) or context.service is not self:
            raise RuntimeError("trusted collaboration context is required")
        if context.role != role:
            raise RuntimeError("collaboration tool is unavailable in this context")

    @staticmethod
    def _require_task_creator(task: dict[str, Any], context: CollaborationAgentContext) -> None:
        if task["creator_account_id"] != context.creator_account_id or task["task_id"] != context.task_id:
            raise RuntimeError("collaboration task creator is inconsistent")

    def _recheck_creator_authorization(self, task: dict[str, Any]) -> None:
        resolved = self.resolver.resolve_pinned(
            account_id=str(task["creator_account_id"]),
            profile_revision=int(task["creator_profile_revision"]),
            profile_fingerprint=str(task["creator_profile_fingerprint"]),
        )
        if not resolved.may_participate:
            raise RuntimeError("collaboration participation is revoked")
        if not resolved.may_create_groups:
            raise RuntimeError("collaboration group creation is not authorized")
        invitee_count = len(self.store.active_memberships(str(task["group_id"]))) - 1
        if (
            resolved.invite_quota is not None
            and invitee_count > int(resolved.invite_quota)
        ):
            raise RuntimeError("collaboration invitation quota exceeded")

    def _deliver_origin_card(self, task_id: str, *, completion: bool) -> None:
        task = self.store.ai_task(task_id)
        delivered = (
            task.get("completion_delivered_at")
            if completion
            else task.get("creation_delivered_at")
        )
        if delivered is not None:
            return
        if task["provider"] == "feishu":
            self.store.ensure_origin_delivery_intent(
                task_id,
                completion=completion,
                worker_owner_key=self.owner_key,
                worker_id=self.worker_id,
                worker_generation=self.worker_generation,
                lease_version=self.lease_version,
                recovery_generation=self.recovery_generation,
            )
            return
        if not callable(self.deliver_web_origin):
            raise RuntimeError("web collaboration origin delivery is unavailable")
        self.deliver_web_origin(task=task, completion=completion)
        self.store.mark_origin_delivered(task_id, completion=completion)

    def respond_approval(self, approval_id: str, choice: str) -> dict[str, Any]:
        if self.scheduler is None:
            raise RuntimeError("collaboration scheduler is unavailable")
        responder = getattr(self.scheduler, "respond_approval", None)
        if not callable(responder):
            raise RuntimeError("collaboration approval adapter is unavailable")
        return {"approval": responder(str(approval_id or "").strip(), str(choice or "").strip())}

    def interrupt_target(self, target_id: str) -> dict[str, Any]:
        if self.scheduler is None:
            raise RuntimeError("collaboration scheduler is unavailable")
        return {"target": self.scheduler.interrupt(target_id)}

    def _resolve_accounts(self, account_ids: Iterable[str]):
        resolved = []
        for account_id in dict.fromkeys(self._account_id(value) for value in account_ids):
            item = self.resolver.resolve_current(account_id)
            if not item.may_participate:
                raise RuntimeError("collaboration participation is revoked")
            resolved.append(item)
        return tuple(resolved)

    @staticmethod
    def _account_id(value: Any) -> str:
        account_id = str(value or "").strip()
        if not account_id:
            raise ValueError("account ID is required")
        return account_id

    @classmethod
    def _public_snapshot(cls, snapshot: dict[str, Any]) -> dict[str, Any]:
        return {
            "group": cls._public_group(dict(snapshot["group"])),
            "memberships": [
                cls._public_membership(dict(item))
                for item in snapshot["memberships"]
            ],
            "events": [cls._public_event(dict(item)) for item in snapshot["events"]],
            "turns": [cls._without_internal(dict(item)) for item in snapshot["turns"]],
            "targets": [cls._without_internal(dict(item)) for item in snapshot["targets"]],
            "approvals": [cls._public_approval(dict(item)) for item in snapshot["approvals"]],
            "attachments": [dict(item) for item in snapshot["attachments"]],
            "reconciliation": dict(snapshot["reconciliation"]),
        }

    @classmethod
    def _public_group(cls, group: dict[str, Any]) -> dict[str, Any]:
        return cls._without_internal(group)

    @classmethod
    def _public_membership(cls, membership: dict[str, Any]) -> dict[str, Any]:
        return cls._without_internal(membership)

    @classmethod
    def _public_event(cls, event: dict[str, Any]) -> dict[str, Any]:
        body = dict(event.get("body") or {})
        body.pop("storage_key", None)
        body.pop("materialized_path", None)
        event["body"] = body
        return cls._without_internal(event)

    @classmethod
    def _public_approval(cls, approval: dict[str, Any]) -> dict[str, Any]:
        request = approval.pop("request_json", "{}")
        if isinstance(request, str):
            import json

            try:
                request = json.loads(request)
            except (TypeError, ValueError):
                request = {}
        approval["request"] = {
            key: value
            for key, value in dict(request or {}).items()
            if key in {"summary", "description", "tool_name", "allow_permanent"}
        }
        return cls._without_internal(approval)

    @staticmethod
    def _without_internal(value: dict[str, Any]) -> dict[str, Any]:
        for key in (
            "hidden_session_id",
            "stored_session_id",
            "source_policy",
            "owner_key",
            "worker_owner_key",
            "worker_id",
            "worker_generation",
            "lease_version",
            "recovery_generation",
            "tool_call_id",
        ):
            value.pop(key, None)
        return value
