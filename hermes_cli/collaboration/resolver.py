"""Trusted Owner Worker resolution of managed collaboration employees."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_cli.channel_connectors.bootstrap import build_channel_identity_store
from hermes_cli.channel_identity import (
    ChannelIdentityStore,
    resolve_employee,
    resolve_employee_profile,
)
from hermes_cli.dashboard_auth.owner_context import owner_context_from_owner_key
from hermes_cli.employee_catalog import BUILTIN_ASSISTANT_SYSTEM_PROMPT
from hermes_cli.employee_policy import (
    canonical_employee_snapshot,
    effective_employee_workspace,
    normalize_employee_source_policy,
)
from hermes_cli.model_registrations import resolve_chat_model_registration

from .models import CollaborationMemberProfile


def collaboration_attachment_prefix(membership_id: str) -> str:
    """Return one membership's read-only attachment capability root."""
    return f"collaboration-attachments/{membership_id}"


def collaboration_member_policy(
    employee_policy: dict[str, Any],
    membership_id: str,
) -> dict[str, Any]:
    """Bind a pinned employee policy to one membership's read-only attachments."""
    snapshot = dict(employee_policy)
    snapshot.pop("snapshot_fingerprint", None)
    knowledge = list(snapshot.get("knowledge_relative_paths") or ())
    attachment_prefix = collaboration_attachment_prefix(membership_id)
    if attachment_prefix not in knowledge:
        knowledge.append(attachment_prefix)
    snapshot["knowledge_relative_paths"] = knowledge
    return canonical_employee_snapshot(snapshot)[0]


@dataclass(frozen=True)
class ResolvedCollaborationEmployee:
    """Current authoritative employee identity, policy, and live authorization."""

    member: CollaborationMemberProfile
    employee_policy: dict[str, Any]
    may_participate: bool
    may_create_groups: bool = False
    may_manage_employees: bool = False
    invite_quota: int | None = 5


class CollaborationEmployeeResolver:
    """Resolve collaboration employees only from Control Plane authority."""

    def __init__(
        self,
        *,
        owner_key: str,
        control_home: str | Path,
        global_home: str | Path,
        connector_config: dict[str, Any],
        store: ChannelIdentityStore | None = None,
    ) -> None:
        self.owner_key = str(owner_key or "").strip()
        self.control_home = Path(control_home).expanduser().resolve()
        self.global_home = Path(global_home).expanduser().resolve()
        if not self.owner_key:
            raise ValueError("owner key is required")
        self.owner = owner_context_from_owner_key(
            self.owner_key,
            global_home=self.global_home,
        )
        self._connector_config = dict(connector_config)
        self._store = store

    def _authority_store(self) -> ChannelIdentityStore:
        if self._store is None:
            self._store = build_channel_identity_store(
                self._connector_config,
                control_home=self.control_home,
                global_home=self.global_home,
            )
        return self._store

    def validate_feishu_origin(
        self,
        *,
        employee_id: str,
        connector_account_id: str,
        binding_id: str,
        conversation_id: str,
        source_kind: str,
        thread_id: str,
        dispatch_scope: str,
    ) -> None:
        """Prove one retained Feishu origin against Control Plane authority."""
        exact_employee = str(employee_id or "").strip()
        exact_connector_account = str(connector_account_id or "").strip()
        exact_binding = str(binding_id or "").strip()
        exact_conversation = str(conversation_id or "").strip()
        exact_source = str(source_kind or "").strip()
        exact_thread = str(thread_id or "")
        exact_scope = str(dispatch_scope or "")
        if exact_source not in {"feishu_direct", "feishu_group"}:
            raise RuntimeError("retained Feishu source kind is invalid")
        if (
            not exact_employee
            or not exact_connector_account
            or not exact_binding
            or not exact_conversation
        ):
            raise RuntimeError("retained Feishu origin identity is incomplete")
        if exact_source == "feishu_direct" and (exact_thread or exact_scope):
            raise RuntimeError("Feishu direct origin scope is invalid")
        store = self._authority_store()
        peer_hash = store.crypto.lookup_hash("conversation:feishu", exact_conversation)
        with store.read() as conn:
            row = conn.execute(
                "SELECT 1 FROM channel_bindings b "
                "JOIN connector_accounts a ON a.account_id=b.account_id "
                "JOIN employee_channel_bindings eb "
                "ON eb.connector_account_id=a.account_id AND eb.provider=a.provider "
                "JOIN employees e ON e.employee_id=eb.employee_id "
                "JOIN owner_bindings o ON o.canonical_user_id=e.canonical_user_id "
                "WHERE b.binding_id=? AND eb.employee_id=? AND b.account_id=? "
                "AND b.peer_lookup_hash=? "
                "AND b.status='active' AND a.provider='feishu' AND a.status='active' "
                "AND eb.lifecycle_status='active' AND e.lifecycle_status='active' "
                "AND o.owner_key=?",
                (
                    exact_binding,
                    exact_employee,
                    exact_connector_account,
                    peer_hash,
                    self.owner_key,
                ),
            ).fetchone()
        if row is None:
            raise RuntimeError("retained Feishu origin does not match binding authority")

    def resolve_current(self, employee_id: str) -> ResolvedCollaborationEmployee:
        """Resolve current identity and runtime policy from durable authority."""
        managed = self._resolve_live_authority(employee_id)
        if managed.employee_kind == "builtin_assistant":
            return self._resolved_builtin(managed)
        profile = resolve_employee_profile(
            self._authority_store(),
            owner=self.owner,
            employee_id=managed.employee_id,
        )
        if profile.lifecycle_status != "active":
            raise RuntimeError("employee profile is unavailable")
        return self._resolved(managed, profile)

    def resolve_pinned(
        self,
        *,
        employee_id: str,
        profile_revision: int,
        profile_fingerprint: str,
    ) -> ResolvedCollaborationEmployee:
        """Resolve an immutable profile revision while rechecking live authority."""
        managed = self._resolve_live_authority(employee_id)
        if managed.employee_kind == "builtin_assistant":
            resolved = self._resolved_builtin(managed)
            if (
                resolved.member.profile_revision != int(profile_revision)
                or resolved.member.profile_fingerprint != str(profile_fingerprint)
            ):
                raise RuntimeError("collaboration member profile fingerprint is inconsistent")
            return resolved
        if not managed.collaboration_policy.may_participate:
            raise RuntimeError("collaboration participation is revoked")
        profile = resolve_employee_profile(
            self._authority_store(),
            owner=self.owner,
            employee_id=managed.employee_id,
            revision=int(profile_revision),
        )
        if profile.fingerprint != str(profile_fingerprint):
            raise RuntimeError("collaboration member profile fingerprint is inconsistent")
        return self._resolved(managed, profile)

    def _resolve_live_authority(self, employee_id: str):
        employee = resolve_employee(
            self._authority_store(),
            owner=self.owner,
            employee_id=employee_id,
        )
        if employee.lifecycle_status != "active":
            raise RuntimeError("employee is unavailable")
        return employee

    def _resolved_builtin(self, managed) -> ResolvedCollaborationEmployee:
        from hermes_cli.config import load_config
        from hermes_cli.model_registrations import get_model_registrations_payload
        from hermes_cli.tools_config import _get_platform_tools, enabled_mcp_server_names

        config = load_config()
        registrations = get_model_registrations_payload()
        active_chat = dict(registrations.get("active", {}).get("chat") or {})
        registration_id = str(active_chat.get("registration_id") or "").strip()
        if not registration_id:
            raise RuntimeError("active Chat model registration is unavailable")
        model = resolve_chat_model_registration(registration_id)
        toolsets = sorted(
            _get_platform_tools(config, "cli", include_default_mcp_servers=False)
            | {"project"}
        )
        snapshot = {
            "schema_version": 1,
            "employee_id": managed.employee_id,
            "profile_revision": 1,
            "source_profile_fingerprint": "builtin-assistant-v1",
            "system_prompt": BUILTIN_ASSISTANT_SYSTEM_PROMPT,
            "model": model,
            "toolsets": toolsets,
            "skills": [],
            "mcp_servers": sorted(enabled_mcp_server_names(config)),
            "workspace_relative_path": "",
            "knowledge_relative_paths": [],
            "max_iterations": 90,
            "max_tokens": None,
            "builtin_assistant": True,
        }
        policy, _ = canonical_employee_snapshot(snapshot)
        return ResolvedCollaborationEmployee(
            member=CollaborationMemberProfile(
                employee_id=managed.employee_id,
                profile_revision=1,
                profile_fingerprint="builtin-assistant-v1",
            ),
            employee_policy=policy,
            may_participate=True,
            may_create_groups=True,
            may_manage_employees=True,
            invite_quota=None,
        )

    @staticmethod
    def _resolved(managed, profile) -> ResolvedCollaborationEmployee:
        source_policy = normalize_employee_source_policy(profile.profile)
        model = resolve_chat_model_registration(source_policy["model_registration_id"])
        snapshot = {
            "schema_version": source_policy["schema_version"],
            "employee_id": managed.employee_id,
            "profile_revision": profile.revision,
            "source_profile_fingerprint": profile.fingerprint,
            "system_prompt": source_policy["system_prompt"],
            "model": model,
            "toolsets": source_policy["toolsets"],
            "skills": source_policy["skills"],
            "mcp_servers": source_policy["mcp_servers"],
            "workspace_relative_path": effective_employee_workspace(
                managed.employee_id,
                source_policy["workspace_relative_path"],
            ),
            "knowledge_relative_paths": source_policy["knowledge_relative_paths"],
            "max_iterations": source_policy["max_iterations"],
            "max_tokens": source_policy["max_tokens"],
            "reasoning_effort": source_policy.get("reasoning_effort", ""),
        }
        employee_policy, _ = canonical_employee_snapshot(snapshot)
        return ResolvedCollaborationEmployee(
            member=CollaborationMemberProfile(
                employee_id=managed.employee_id,
                profile_revision=profile.revision,
                profile_fingerprint=profile.fingerprint,
            ),
            employee_policy=employee_policy,
            may_participate=managed.collaboration_policy.may_participate,
            may_create_groups=managed.collaboration_policy.may_create_groups,
            may_manage_employees=False,
            invite_quota=managed.collaboration_policy.invite_quota,
        )
