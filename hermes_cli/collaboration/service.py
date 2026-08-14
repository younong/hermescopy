"""Trusted Owner Worker service for internal collaboration groups."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict
import hashlib
import uuid
from typing import Any, Callable, Protocol

from hermes_cli.attachment_uploads import validate_upload
from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext
from hermes_cli.channel_identity import (
    create_employee,
    list_employees,
    resolve_employee_profile,
)
from hermes_cli.controlled_roots import RootKind
from hermes_cli.employee_catalog import employee_catalog_payload
from hermes_cli.employee_policy import normalize_employee_source_policy
from hermes_state import SessionDB

from .agent_tools import CollaborationAgentContext
from .parser import parse_discussion_round_count
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
        employee_ids: Iterable[str] = (),
        client_idempotency_key: str,
    ) -> dict[str, Any]:
        resolved = self._resolve_employees(employee_ids)
        resolved = tuple(sorted(resolved, key=lambda item: item.member.employee_id))
        policies = {item.member.employee_id: item.employee_policy for item in resolved}

        def _provision(membership, member) -> None:
            policy = collaboration_member_policy(
                policies[member.employee_id], membership.membership_id
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
                policies[membership.employee_id], membership.membership_id
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
        employee_ids: Iterable[str],
    ) -> dict[str, Any]:
        requested = tuple(dict.fromkeys(self._employee_id(value) for value in employee_ids))
        current = {item.employee_id: item for item in self.store.active_memberships(group_id)}
        requested_set = set(requested)
        resolved_policies: dict[str, dict[str, Any]] = {}
        for employee_id in requested:
            membership = current.get(employee_id)
            if membership is None:
                continue
            resolved = self.resolver.resolve_pinned(
                employee_id=membership.employee_id,
                profile_revision=membership.profile_revision,
                profile_fingerprint=membership.profile_fingerprint,
            )
            if not resolved.may_participate:
                raise RuntimeError("collaboration participation is revoked")
            resolved_policies[employee_id] = resolved.employee_policy
        additions = {
            item.member.employee_id: item
            for item in self._resolve_employees(
                employee_id for employee_id in requested if employee_id not in current
            )
        }
        resolved_policies.update(
            {employee_id: item.employee_policy for employee_id, item in additions.items()}
        )

        def _provision(membership, member) -> None:
            policy = collaboration_member_policy(
                resolved_policies[member.employee_id], membership.membership_id
            )
            if self.provision_member_session is not None:
                self.provision_member_session(membership, policy)

        memberships = self.store.update_memberships(
            group_id,
            requested_employee_ids=requested,
            additions={employee_id: item.member for employee_id, item in additions.items()},
            provision_member=_provision,
        )
        for membership in memberships:
            policy = additions.get(membership.employee_id)
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
            total_rounds=parse_discussion_round_count(text),
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
        creator_employee_id: str,
        source_kind: str,
        source_conversation_id: str,
        source_provider: str = "web",
        source_connector_account_id: str | None = None,
        source_binding_id: str | None = None,
        source_thread_id: str = "",
        source_session_id: str | None = None,
        source_group_id: str | None = None,
        source_event_id: str | None = None,
        allowed_origin_attachment_ids: Iterable[str] = (),
        require_participation: bool = True,
    ) -> CollaborationAgentContext:
        """Build one trusted source context after resolving live authorization."""
        creator = self.resolver.resolve_current(self._employee_id(creator_employee_id))
        if require_participation and not creator.may_participate:
            raise RuntimeError("collaboration participation is revoked")
        return CollaborationAgentContext(
            service=self,
            creator_employee_id=creator.member.employee_id,
            source_kind=str(source_kind),
            source_conversation_id=str(source_conversation_id),
            source_provider=str(source_provider),
            source_connector_account_id=source_connector_account_id,
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
            may_manage_employees=bool(creator.may_manage_employees),
        )

    def list_employee_catalog(
        self, *, context: CollaborationAgentContext
    ) -> dict[str, Any]:
        """Return the live Owner catalog after rechecking built-in authority."""
        self._require_employee_manager(context)
        catalog = employee_catalog_payload(self.resolver.owner.host_owner_home)
        catalog["employees"] = [
            {
                "employee_id": item.employee_id,
                "employee_kind": item.employee_kind,
                "lifecycle_status": item.lifecycle_status,
                "name": self._employee_display_name(item.employee_id),
            }
            for item in list_employees(
                self.resolver._authority_store(), owner=self.resolver.owner
            )
        ]
        return {"catalog": catalog}

    def create_managed_employee(
        self,
        *,
        context: CollaborationAgentContext,
        policy: dict[str, Any],
    ) -> dict[str, Any]:
        """Create one catalog-validated employee through controlled Owner APIs."""
        self._require_employee_manager(context)
        if not isinstance(policy, dict):
            raise ValueError("employee policy must be an object")
        normalized = normalize_employee_source_policy(policy)
        catalog = employee_catalog_payload(self.resolver.owner.host_owner_home)
        self._validate_employee_policy_catalog(normalized, catalog)
        filesystem = self.filesystem_context
        if not isinstance(filesystem, AuthenticatedWorkspaceContext):
            raise RuntimeError("authenticated employee workspace is unavailable")
        controlled_workspace = filesystem.controlled_workspace_path(
            normalized["workspace_relative_path"]
        )
        filesystem.roots.mkdirs(RootKind.WORKSPACE, controlled_workspace)
        employee = create_employee(
            self.resolver._authority_store(),
            owner=self.resolver.owner,
            profile=normalized,
        )
        return {
            "employee": {
                "employee_id": employee.employee_id,
                "employee_kind": employee.employee_kind,
                "lifecycle_status": employee.lifecycle_status,
                "name": str(normalized.get("name") or ""),
                "role": str(normalized.get("role") or ""),
                "profile_revision": employee.profile_revision,
                "profile_fingerprint": employee.profile_fingerprint,
                "workspace_relative_path": normalized["workspace_relative_path"],
            }
        }

    def create_internal_group(
        self,
        *,
        context: CollaborationAgentContext,
        title: str,
        brief: str,
        invitee_employee_ids: Iterable[str],
        origin_attachment_ids: Iterable[str],
        first_round_target_employee_ids: Iterable[str],
        idempotency_key: str,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        del tool_call_id
        self._require_bound_context(context, role="source")
        if int(context.source_depth) != 0 or context.source_task_id is not None:
            raise RuntimeError("nested collaboration group creation is unavailable")
        creator = self.resolver.resolve_current(context.creator_employee_id)
        if not creator.may_participate:
            raise RuntimeError("collaboration participation is revoked")
        if not creator.may_create_groups:
            raise RuntimeError("collaboration group creation is not authorized")
        invitees = tuple(dict.fromkeys(self._employee_id(value) for value in invitee_employee_ids))
        if creator.invite_quota is not None and len(invitees) > int(creator.invite_quota):
            raise RuntimeError("collaboration invitation quota exceeded")
        targets = tuple(
            dict.fromkeys(self._employee_id(value) for value in first_round_target_employee_ids)
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
        resolved = self._resolve_employees(invitees)
        result, created = self.store.create_ai_task(
            title=title,
            brief=brief,
            creator=creator.member,
            members=[item.member for item in resolved],
            source_kind=context.source_kind,
            source_provider=context.source_provider,
            source_connector_account_id=context.source_connector_account_id,
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
            creator.member.employee_id: creator,
            **{item.member.employee_id: item for item in resolved},
        }
        for membership in active:
            item = policies[membership.employee_id]
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
                    creator_employee_id=context.creator_employee_id,
                    source_kind=context.source_kind,
                    source_conversation_id=context.source_conversation_id,
                    source_provider=context.source_provider,
                    source_connector_account_id=context.source_connector_account_id,
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
                target_employee_ids=targets,
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
        target_employee_ids: Iterable[str],
        attachment_ids: Iterable[str],
        idempotency_key: str,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        del tool_call_id
        self._require_bound_context(context, role="coordinator")
        task = self.store.ai_task(str(context.task_id or ""))
        self._require_task_creator(task, context)
        self._recheck_creator_authorization(task)
        target_employees = tuple(
            dict.fromkeys(self._employee_id(value) for value in target_employee_ids)
        )
        memberships = {
            item.employee_id: item for item in self.store.active_memberships(task["group_id"])
        }
        if not target_employees or not set(target_employees) <= set(memberships):
            raise RuntimeError("collaboration target is not an active member")
        selected = tuple(dict.fromkeys(str(value or "").strip() for value in attachment_ids))
        if any(not value for value in selected) or not set(selected) <= set(task["allowed_attachment_ids"]):
            raise RuntimeError("collaboration attachment is not allowed for this task")
        for employee_id in target_employees:
            membership = memberships[employee_id]
            resolved = self.resolver.resolve_pinned(
                employee_id=membership.employee_id,
                profile_revision=membership.profile_revision,
                profile_fingerprint=membership.profile_fingerprint,
            )
            if not resolved.may_participate:
                raise RuntimeError("collaboration participation is revoked")
        submitted, next_round, created = self.store.dispatch_ai_round(
            task["task_id"],
            instruction=instruction,
            target_employee_ids=target_employees,
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

    def _require_employee_manager(self, context: CollaborationAgentContext):
        self._require_bound_context(context, role="source")
        if int(context.source_depth) != 0 or context.source_task_id is not None:
            raise RuntimeError("employee management is unavailable in this context")
        resolved = self.resolver.resolve_current(context.creator_employee_id)
        if not resolved.may_manage_employees:
            raise RuntimeError("employee management is not authorized")
        return resolved

    @staticmethod
    def _validate_employee_policy_catalog(
        policy: dict[str, Any], catalog: dict[str, Any]
    ) -> None:
        chat_ids = {
            str(item.get("id") or item.get("registration_id") or "")
            for item in catalog.get("model_registrations", [])
        }
        toolsets = {str(item.get("name") or "") for item in catalog.get("toolsets", [])}
        skills = {str(item.get("name") or "") for item in catalog.get("skills", [])}
        mcp_servers = {str(item) for item in catalog.get("mcp_servers", [])}
        if policy["model_registration_id"] not in chat_ids:
            raise ValueError("model registration is unavailable")
        unknown_toolsets = sorted(set(policy["toolsets"]) - toolsets)
        if unknown_toolsets:
            raise ValueError(f"unknown or disabled toolset: {unknown_toolsets[0]}")
        unknown_skills = sorted(set(policy["skills"]) - skills)
        if unknown_skills:
            raise ValueError(f"unknown or disabled skill: {unknown_skills[0]}")
        unknown_mcp = sorted(set(policy["mcp_servers"]) - mcp_servers)
        if unknown_mcp:
            raise ValueError(f"unknown or disabled MCP server: {unknown_mcp[0]}")

    def _employee_display_name(self, employee_id: str) -> str:
        try:
            resolved = self.resolver.resolve_current(employee_id)
            if resolved.may_manage_employees:
                return "AI Assistant"
            current = resolve_employee_profile(
                self.resolver._authority_store(),
                owner=self.resolver.owner,
                employee_id=employee_id,
            )
            return str(current.profile.get("name") or "")
        except (RuntimeError, ValueError):
            return ""

    @staticmethod
    def _require_task_creator(task: dict[str, Any], context: CollaborationAgentContext) -> None:
        if task["creator_employee_id"] != context.creator_employee_id or task["task_id"] != context.task_id:
            raise RuntimeError("collaboration task creator is inconsistent")

    def _recheck_creator_authorization(self, task: dict[str, Any]) -> None:
        resolved = self.resolver.resolve_pinned(
            employee_id=str(task["creator_employee_id"]),
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

    def _resolve_employees(self, employee_ids: Iterable[str]):
        resolved = []
        for employee_id in dict.fromkeys(self._employee_id(value) for value in employee_ids):
            item = self.resolver.resolve_current(employee_id)
            if not item.may_participate:
                raise RuntimeError("collaboration participation is revoked")
            resolved.append(item)
        return tuple(resolved)

    @staticmethod
    def _employee_id(value: Any) -> str:
        employee_id = str(value or "").strip()
        if not employee_id:
            raise ValueError("employee ID is required")
        return employee_id

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
