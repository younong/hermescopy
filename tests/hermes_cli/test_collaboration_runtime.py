from __future__ import annotations

import json
from types import SimpleNamespace
import threading
import time
from dataclasses import dataclass

import pytest

from hermes_cli.collaboration.agent_tools import CollaborationAgentContext, invoke, tool_definitions
from hermes_cli.collaboration.models import CollaborationMemberProfile
from hermes_cli.collaboration.resolver import ResolvedCollaborationEmployee
from hermes_cli.collaboration.scheduler import CollaborationScheduler
from hermes_cli.collaboration.service import CollaborationService
from hermes_cli.collaboration.store import CollaborationStore
from hermes_state import SessionDB


@dataclass(frozen=True)
class _Runtime:
    owner_key: str = "owner-a"
    worker_generation: int = 2
    worker_id: str = "worker-a"
    lease_version: int = 3
    recovery_generation: int = 4


def _policy(employee_id: str, revision: int = 1) -> dict:
    return {
        "schema_version": 1,
        "employee_id": employee_id,
        "profile_revision": revision,
        "source_profile_fingerprint": f"fingerprint-{employee_id}-r{revision}",
        "system_prompt": f"You are {employee_id}.",
        "model": {"provider": "openai", "model": "test-model"},
        "toolsets": [],
        "skills": [],
        "mcp_servers": [],
        "workspace_relative_path": "",
        "knowledge_relative_paths": [],
        "max_iterations": 4,
        "max_tokens": 128,
    }


class _Resolver:
    def __init__(
        self,
        *,
        revision: int = 1,
        allowed: bool = True,
        may_create: bool = False,
        invite_quota: int | None = 5,
    ) -> None:
        self.revision = revision
        self.allowed = allowed
        self.may_create = may_create
        self.invite_quota = invite_quota
        self.resolved_employees: list[str] = []
        self.pinned_employees: list[tuple[str, int]] = []

    def resolve_current(self, employee_id: str) -> ResolvedCollaborationEmployee:
        self.resolved_employees.append(employee_id)
        member = CollaborationMemberProfile(
            employee_id=employee_id,
            profile_revision=self.revision,
            profile_fingerprint=f"fingerprint-{employee_id}-r{self.revision}",
        )
        return ResolvedCollaborationEmployee(
            member=member,
            employee_policy=_policy(employee_id, self.revision),
            may_participate=self.allowed,
            may_create_groups=self.may_create,
            invite_quota=self.invite_quota,
        )

    def resolve_pinned(self, *, employee_id, profile_revision, profile_fingerprint):
        self.pinned_employees.append((employee_id, profile_revision))
        if not self.allowed:
            raise RuntimeError("collaboration participation is revoked")
        expected = f"fingerprint-{employee_id}-r{profile_revision}"
        if profile_fingerprint != expected:
            raise RuntimeError("collaboration member profile fingerprint is inconsistent")
        return ResolvedCollaborationEmployee(
            member=CollaborationMemberProfile(employee_id, profile_revision, expected),
            employee_policy=_policy(employee_id, profile_revision),
            may_participate=True,
            may_create_groups=self.may_create,
            invite_quota=self.invite_quota,
        )


class _ApprovalRunner:
    def __init__(self):
        self.started = threading.Event()
        self.waiting = threading.Event()
        self.resume = threading.Event()
        self.interrupted: list[str] = []

    def run(self, **kwargs):
        self.started.set()
        kwargs["on_approval"](
            {
                "tool_call_id": "tool-call-a",
                "tool_name": "terminal",
                "description": "run safe command",
            }
        )
        self.waiting.set()
        self.resume.wait(timeout=2)
        return {"status": "complete", "text": "approved reply"}

    def interrupt(self, hidden_session_id: str) -> bool:
        self.interrupted.append(hidden_session_id)
        self.resume.set()
        return True

    def close(self):
        self.resume.set()


class _Runner:
    def __init__(self, *, text: str = "employee reply", block: threading.Event | None = None):
        self.text = text
        self.block = block
        self.started = threading.Event()
        self.interrupted: list[str] = []
        self.calls: list[dict] = []
        self.service = None

    def bind_service(self, service) -> None:
        self.service = service

    def ensure_coordinator_session(self, *, task_id: str, employee_policy: dict):
        return f"coordinator-{task_id}", f"hidden-{task_id}"

    def run(self, **kwargs):
        self.calls.append(kwargs)
        self.started.set()
        kwargs["on_delta"]("delta")
        if self.block is not None:
            self.block.wait(timeout=2)
        return {"status": "complete", "text": self.text}

    def interrupt(self, hidden_session_id: str) -> bool:
        self.interrupted.append(hidden_session_id)
        if self.block is not None:
            self.block.set()
        return True

    def close(self):
        return None


@pytest.fixture
def db(tmp_path):
    value = SessionDB(tmp_path / "state.db")
    try:
        yield value
    finally:
        value.close()


def test_agent_tool_contract_rejects_forged_context_and_unknown_arguments():
    assert tool_definitions(role="source", may_create=False) == []
    assert [
        tool["function"]["name"]
        for tool in tool_definitions(role="source", may_create=True)
    ] == ["create_internal_group"]
    assert [
        tool["function"]["name"]
        for tool in tool_definitions(role="coordinator")
    ] == ["dispatch_internal_group_round", "finish_internal_group_task"]
    assert tool_definitions(role="member") == []
    unavailable = invoke(
        CollaborationAgentContext(
            service=object(),
            creator_employee_id="employee-a",
            source_kind="web_direct",
            source_conversation_id="session-a",
        ),
        "create_internal_group",
        {
            "title": "Task",
            "brief": "Brief",
            "invitee_employee_ids": ["employee-b"],
            "origin_attachment_ids": [],
            "first_round_target_employee_ids": ["employee-b"],
            "idempotency_key": "create-a",
            "owner_key": "forged",
        },
        tool_call_id="tool-a",
    )
    assert json.loads(unavailable) == {
        "success": False,
        "error": "collaboration tool arguments are invalid",
    }


def test_employee_management_tools_require_durable_manager_authority(db):
    class _ManagementResolver(_Resolver):
        def __init__(self):
            super().__init__(may_create=True, invite_quota=None)
            self.may_manage = True

        def resolve_current(self, employee_id: str) -> ResolvedCollaborationEmployee:
            resolved = super().resolve_current(employee_id)
            return ResolvedCollaborationEmployee(
                member=resolved.member,
                employee_policy=resolved.employee_policy,
                may_participate=resolved.may_participate,
                may_create_groups=resolved.may_create_groups,
                may_manage_employees=self.may_manage,
                invite_quota=resolved.invite_quota,
            )

    resolver = _ManagementResolver()
    service = CollaborationService(
        db,
        owner_key="owner-a",
        resolver=resolver,
        emit=lambda *_args: None,
        ensure_member_session=lambda **_kwargs: None,
    )
    context = service.source_agent_context(
        creator_employee_id="assistant",
        source_kind="web_direct",
        source_conversation_id="session-a",
    )

    assert context.may_manage_employees is True
    assert [
        tool["function"]["name"]
        for tool in tool_definitions(
            role="source",
            may_create=True,
            may_manage_employees=True,
        )
    ] == [
        "create_internal_group",
        "list_employee_catalog",
        "create_managed_employee",
    ]

    resolver.may_manage = False
    with pytest.raises(RuntimeError, match="not authorized"):
        service.list_employee_catalog(context=context)


def test_source_context_checks_live_creation_authority_and_quota(db):
    resolver = _Resolver(may_create=False)
    service = CollaborationService(
        db,
        owner_key="owner-a",
        resolver=resolver,
        emit=lambda *_args: None,
        ensure_member_session=lambda **_kwargs: None,
    )
    denied = service.source_agent_context(
        creator_employee_id="creator",
        source_kind="web_group",
        source_conversation_id="source-group",
        source_group_id="source-group",
    )
    assert denied.may_create_authorized is False

    resolver.may_create = True
    resolver.invite_quota = None
    allowed = service.source_agent_context(
        creator_employee_id="creator",
        source_kind="web_group",
        source_conversation_id="source-group",
        source_group_id="source-group",
    )
    assert allowed.may_create_authorized is True


def test_create_managed_employee_validates_catalog_and_controlled_workspace(
    db, tmp_path, monkeypatch
):
    from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext
    from hermes_cli.channel_identity import ChannelCrypto, ChannelIdentityStore, Keyring
    from hermes_cli.collaboration import service as service_module
    from hermes_cli.controlled_roots import RootKind, controlled_roots_for
    from hermes_cli.dashboard_auth.owner_context import owner_context_from_owner_key
    from hermes_cli.owner_runtime import ensure_owner_runtime_dirs, owner_worker_runtime_paths

    import hermes_cli.controlled_roots as controlled_roots

    monkeypatch.setattr(controlled_roots.sys, "platform", "linux")
    monkeypatch.setattr(controlled_roots, "_openat2", lambda *_args: None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_OWNER_SECRET", "owner-secret")
    owner_home = ensure_owner_runtime_dirs(tmp_path / "users" / "ok1_owner-a")
    paths = owner_worker_runtime_paths(owner_home=owner_home, worker_generation=1)
    paths.default_workspace.mkdir(parents=True, exist_ok=True)
    roots = controlled_roots_for(paths)
    filesystem = AuthenticatedWorkspaceContext(roots)
    identity_store = ChannelIdentityStore(
        ChannelCrypto(
            lookup=Keyring(keys={1: b"l" * 32}, active_version=1),
            encryption=Keyring(keys={1: b"e" * 32}, active_version=1),
        ),
        tmp_path / "control",
        global_home=tmp_path,
    )

    class _ManagementResolver(_Resolver):
        owner = owner_context_from_owner_key("ok1_owner-a", global_home=tmp_path)

        def _authority_store(self):
            return identity_store

        def resolve_current(self, employee_id: str) -> ResolvedCollaborationEmployee:
            resolved = super().resolve_current(employee_id)
            return ResolvedCollaborationEmployee(
                member=resolved.member,
                employee_policy=resolved.employee_policy,
                may_participate=True,
                may_create_groups=True,
                may_manage_employees=True,
                invite_quota=None,
            )

    catalog = {
        "model_registrations": [{"id": "model-a"}],
        "active_chat": {"registration_id": "model-a"},
        "toolsets": [{"name": "terminal"}],
        "skills": [{"name": "research"}],
        "mcp_servers": ["browser"],
        "workspace": {"root": "", "default": "default"},
        "knowledge_roots": [],
    }
    monkeypatch.setattr(service_module, "employee_catalog_payload", lambda _home: catalog)
    resolver = _ManagementResolver(may_create=True, invite_quota=None)
    service = CollaborationService(
        db,
        owner_key="owner-a",
        resolver=resolver,
        emit=lambda *_args: None,
        ensure_member_session=lambda **_kwargs: None,
        filesystem_context=filesystem,
    )
    context = service.source_agent_context(
        creator_employee_id="assistant",
        source_kind="web_direct",
        source_conversation_id="session-a",
    )
    policy = {
        "schema_version": 1,
        "name": "Researcher",
        "role": "Analyst",
        "model_registration_id": "model-a",
        "system_prompt": "Research carefully.",
        "toolsets": ["terminal"],
        "skills": ["research"],
        "mcp_servers": ["browser"],
        "workspace_relative_path": "employees/researcher",
        "knowledge_relative_paths": [],
        "max_iterations": 20,
        "max_tokens": None,
    }

    created = service.create_managed_employee(context=context, policy=policy)

    assert created["employee"]["employee_kind"] == "managed"
    assert created["employee"]["name"] == "Researcher"
    assert roots.list_directory(RootKind.WORKSPACE, "default/employees")[0].name == "researcher"
    with pytest.raises(ValueError, match="unknown or disabled skill"):
        service.create_managed_employee(
            context=context,
            policy={**policy, "skills": ["missing"]},
        )


def test_create_and_later_actions_recheck_live_permission_and_quota(db):
    resolver = _Resolver(may_create=True, invite_quota=0)
    service = CollaborationService(
        db,
        owner_key="owner-a",
        resolver=resolver,
        emit=lambda *_args: None,
        ensure_member_session=lambda **_kwargs: None,
        deliver_web_origin=lambda **_kwargs: None,
    )
    service.bind_scheduler(SimpleNamespace(wake=lambda: None))
    context = service.source_agent_context(
        creator_employee_id="creator",
        source_kind="web_direct",
        source_conversation_id="origin-session",
    )
    request = {
        "context": context,
        "title": "Live authority",
        "brief": "Check permissions",
        "invitee_employee_ids": ["employee-a"],
        "origin_attachment_ids": [],
        "first_round_target_employee_ids": ["employee-a"],
        "idempotency_key": "live-authority",
    }
    with pytest.raises(RuntimeError, match="quota exceeded"):
        service.create_internal_group(**request)

    resolver.invite_quota = None
    created = service.create_internal_group(**request)
    coordinator = CollaborationAgentContext(
        service=service,
        creator_employee_id="creator",
        source_kind="web_direct",
        source_conversation_id="origin-session",
        source_depth=1,
        task_id=created["task_id"],
        role="coordinator",
    )
    with db._lock:
        db._conn.execute(
            "UPDATE collaboration_turns SET status='completed' WHERE group_id=?",
            (created["group_id"],),
        )
        db._conn.commit()

    resolver.may_create = False
    with pytest.raises(RuntimeError, match="not authorized"):
        service.dispatch_internal_group_round(
            context=coordinator,
            instruction="Another round",
            target_employee_ids=["employee-a"],
            attachment_ids=[],
            idempotency_key="live-authority-round-2",
        )
    with pytest.raises(RuntimeError, match="not authorized"):
        service.finish_internal_group_task(
            context=coordinator,
            summary="Do not finish",
            idempotency_key="live-authority-finish",
        )

    resolver.may_create = True
    resolver.invite_quota = 0
    with pytest.raises(RuntimeError, match="quota exceeded"):
        service.dispatch_internal_group_round(
            context=coordinator,
            instruction="Still over quota",
            target_employee_ids=["employee-a"],
            attachment_ids=[],
            idempotency_key="live-authority-round-2-quota",
        )


def test_create_replay_recovers_initial_dispatch_after_post_create_failure(db, monkeypatch):
    resolver = _Resolver(may_create=True)
    scheduler = SimpleNamespace(wake=lambda: None)
    service = CollaborationService(
        db,
        owner_key="owner-a",
        resolver=resolver,
        emit=lambda *_args: None,
        ensure_member_session=lambda **_kwargs: None,
        deliver_web_origin=lambda **_kwargs: None,
    )
    service.bind_scheduler(scheduler)
    context = service.source_agent_context(
        creator_employee_id="creator",
        source_kind="web_direct",
        source_conversation_id="origin-session",
    )
    original_dispatch = service.dispatch_internal_group_round
    calls = 0

    def _fail_once(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected post-create failure")
        return original_dispatch(**kwargs)

    monkeypatch.setattr(service, "dispatch_internal_group_round", _fail_once)
    request = {
        "context": context,
        "title": "Recoverable review",
        "brief": "Review once",
        "invitee_employee_ids": ["employee-a"],
        "origin_attachment_ids": [],
        "first_round_target_employee_ids": ["employee-a"],
        "idempotency_key": "recover-create",
    }
    with pytest.raises(RuntimeError, match="post-create failure"):
        service.create_internal_group(**request)

    recovered = service.create_internal_group(**request)

    assert recovered["round"] == 1
    assert calls == 2
    with db._lock:
        assert db._conn.execute(
            "SELECT COUNT(*) FROM collaboration_groups WHERE creator_kind='employee'"
        ).fetchone()[0] == 1
        assert db._conn.execute(
            "SELECT COUNT(*) FROM collaboration_turns"
        ).fetchone()[0] == 1


def test_service_resolves_profiles_server_side_and_redacts_hidden_ids(db):
    sessions = []
    emitted = []
    service = CollaborationService(
        db,
        owner_key="owner-a",
        resolver=_Resolver(),
        emit=lambda event, payload: emitted.append((event, payload)),
        ensure_member_session=lambda **kwargs: sessions.append(kwargs),
    )

    payload = service.create_group(
        name="Runtime",
        employee_ids=["employee-a"],
        client_idempotency_key="runtime-create",
    )

    assert payload["memberships"][0]["employee_id"] == "employee-a"
    assert "hidden_session_id" not in payload["memberships"][0]
    assert "stored_session_id" not in payload["memberships"][0]
    assert sessions[0]["employee_policy"]["employee_id"] == "employee-a"
    assert emitted[-1][0] == "collaboration.group.changed"
    serialized = json.dumps(payload, sort_keys=True)
    for internal in (
        "owner_key",
        "hidden_session_id",
        "stored_session_id",
        "storage_key",
        "materialized_path",
        "source_policy",
        "worker_id",
        "worker_generation",
        "lease_version",
        "recovery_generation",
    ):
        assert internal not in serialized


def test_group_create_retry_repairs_sessions_without_duplicate_group(db):
    sessions = []
    emits = 0

    def flaky_emit(*_args):
        nonlocal emits
        emits += 1
        if emits == 1:
            raise RuntimeError("emit failed")

    service = CollaborationService(
        db,
        owner_key="owner-a",
        resolver=_Resolver(),
        emit=flaky_emit,
        ensure_member_session=lambda **kwargs: sessions.append(kwargs),
    )
    request = {
        "name": "Retry",
        "employee_ids": ["employee-a"],
        "client_idempotency_key": "retry-create",
    }

    with pytest.raises(RuntimeError, match="emit failed"):
        service.create_group(**request)
    recovered = service.create_group(**request)

    assert len(sessions) == 2
    with db._lock:
        assert db._conn.execute(
            "SELECT COUNT(*) FROM collaboration_groups WHERE name='Retry'"
        ).fetchone()[0] == 1
        assert db._conn.execute(
            "SELECT COUNT(*) FROM collaboration_memberships WHERE group_id=?",
            (recovered["group"]["group_id"],),
        ).fetchone()[0] == 1


def test_member_update_preserves_existing_pinned_revision(db):
    resolver = _Resolver()
    sessions = []
    service = CollaborationService(
        db,
        owner_key="owner-a",
        resolver=resolver,
        emit=lambda *_args: None,
        ensure_member_session=lambda **kwargs: sessions.append(kwargs),
    )
    created = service.create_group(
        name="Runtime",
        employee_ids=["employee-a"],
        client_idempotency_key="runtime-create",
    )
    original = created["memberships"][0]
    resolver.revision = 2

    updated = service.update_members(
        created["group"]["group_id"], employee_ids=["employee-a"]
    )

    active = [item for item in updated["memberships"] if item["leave_sequence"] is None]
    assert resolver.resolved_employees == ["employee-a"]
    assert resolver.pinned_employees == [("employee-a", 1)]
    assert len(active) == 1
    assert active[0]["membership_id"] == original["membership_id"]
    assert active[0]["profile_revision"] == 1
    assert len(sessions) == 1


def test_member_update_resolves_all_additions_before_mutating(db):
    resolver = _Resolver()
    service = CollaborationService(
        db,
        owner_key="owner-a",
        resolver=resolver,
        emit=lambda *_args: None,
        ensure_member_session=lambda **_kwargs: None,
    )
    created = service.create_group(
        name="Runtime",
        employee_ids=["employee-a"],
        client_idempotency_key="runtime-create",
    )
    original = service.get_group(created["group"]["group_id"])
    original_resolve = resolver.resolve_current

    def fail_second(employee_id):
        if employee_id == "employee-c":
            raise RuntimeError("employee unavailable")
        return original_resolve(employee_id)

    resolver.resolve_current = fail_second
    with pytest.raises(RuntimeError, match="employee unavailable"):
        service.update_members(
            created["group"]["group_id"],
            employee_ids=["employee-b", "employee-c"],
        )

    after = service.get_group(created["group"]["group_id"])
    assert after["memberships"] == original["memberships"]
    assert after["events"] == original["events"]


def test_member_update_rechecks_live_authorization_without_repinning(db):
    resolver = _Resolver()
    service = CollaborationService(
        db,
        owner_key="owner-a",
        resolver=resolver,
        emit=lambda *_args: None,
        ensure_member_session=lambda **_kwargs: None,
    )
    created = service.create_group(
        name="Runtime",
        employee_ids=["employee-a"],
        client_idempotency_key="runtime-create",
    )
    resolver.revision = 2
    resolver.allowed = False

    with pytest.raises(RuntimeError, match="participation is revoked"):
        service.update_members(
            created["group"]["group_id"], employee_ids=["employee-a"]
        )

    active = service.get_group(created["group"]["group_id"])["memberships"]
    assert resolver.pinned_employees == [("employee-a", 1)]
    assert active[0]["profile_revision"] == 1
    assert active[0]["leave_sequence"] is None


def test_feishu_origin_persists_stable_delivery_intent_before_handoff(db):
    resolver = _Resolver(may_create=True)
    service = CollaborationService(
        db,
        owner_key="owner-a",
        resolver=resolver,
        emit=lambda *_args: None,
        ensure_member_session=lambda **_kwargs: None,
        worker_id="worker-a",
        worker_generation=2,
        lease_version=3,
        recovery_generation=4,
    )
    service.bind_scheduler(SimpleNamespace(wake=lambda: None))
    context = service.source_agent_context(
        creator_employee_id="creator",
        source_kind="feishu_direct",
        source_provider="feishu",
        source_connector_account_id="creator",
        source_binding_id="binding-a",
        source_conversation_id="oc_direct",
        source_thread_id="",
        source_session_id="session-a",
    )

    result = service.create_internal_group(
        context=context,
        title="Feishu review",
        brief="Only explicit brief",
        invitee_employee_ids=["employee-a"],
        origin_attachment_ids=[],
        first_round_target_employee_ids=["employee-a"],
        idempotency_key="feishu-create",
    )
    pending = service.store.pending_origin_deliveries()
    assert len(pending) == 1
    delivery = pending[0]
    assert delivery["delivery_key"] == f"collaboration:{result['task_id']}:creation"
    assert delivery["provider"] == "feishu"
    assert delivery["employee_id"] == "creator"
    assert delivery["connector_account_id"] == "creator"
    assert delivery["binding_id"] == "binding-a"
    assert delivery["conversation_id"] == "oc_direct"
    assert delivery["thread_id"] == ""
    assert delivery["source_session_id"] == "session-a"
    assert delivery["worker_id"] == "worker-a"
    assert "Feishu review" in delivery["payload_text"]
    assert "Only explicit brief" not in delivery["payload_text"]

    # A restart/replay sees the durable intent and never creates another row.
    service._deliver_origin_card(result["task_id"], completion=False)
    assert len(service.store.pending_origin_deliveries()) == 1
    assert service.store.record_origin_delivery_result(
        delivery["delivery_key"], outbound_id="om_stable"
    ) is True
    pending_after_enqueue = service.store.pending_origin_deliveries()
    assert pending_after_enqueue[0]["status"] == "claimed"
    assert pending_after_enqueue[0]["outbound_id"] == "om_stable"
    assert service.store.record_origin_delivery_result(
        delivery["delivery_key"],
        outbound_id="om_stable",
        result_status="delivered",
    ) is True
    assert service.store.pending_origin_deliveries() == []


def test_restart_adopts_pending_and_claimed_delivery_intents_to_exact_fence(db):
    store = CollaborationStore(db, owner_key="owner-a")
    created, _ = store.create_ai_task(
        title="Restart delivery",
        brief="Brief",
        creator=CollaborationMemberProfile("creator", 1, "fingerprint-creator-r1"),
        members=[CollaborationMemberProfile("employee-a", 1, "fingerprint-employee-a-r1")],
        source_kind="feishu_direct",
        source_provider="feishu",
        source_connector_account_id="creator",
        source_binding_id="binding-a",
        source_conversation_id="oc_direct",
        source_thread_id="",
        source_session_id="session-a",
        source_group_id=None,
        source_event_id=None,
        source_task_id=None,
        depth=1,
        allowed_attachment_ids=(),
        idempotency_key="restart-delivery",
    )
    pending = store.ensure_origin_delivery_intent(
        created["task_id"],
        completion=False,
        worker_owner_key="owner-a",
        worker_id="old-worker",
        worker_generation=1,
        lease_version=1,
        recovery_generation=0,
    )
    store.complete_ai_task(
        created["task_id"],
        summary="Completed",
        idempotency_key="restart-complete",
    )
    claimed = store.ensure_origin_delivery_intent(
        created["task_id"],
        completion=True,
        worker_owner_key="owner-a",
        worker_id="old-worker",
        worker_generation=1,
        lease_version=1,
        recovery_generation=0,
    )
    assert store.record_origin_delivery_result(
        claimed["delivery_key"], outbound_id="om_claimed"
    )

    adopted = store.pending_origin_deliveries(
        worker_owner_key="owner-a",
        worker_id="new-worker",
        worker_generation=2,
        lease_version=3,
        recovery_generation=4,
    )

    assert {row["delivery_key"] for row in adopted} == {
        pending["delivery_key"],
        claimed["delivery_key"],
    }
    assert {row["status"] for row in adopted} == {"pending", "claimed"}
    assert next(row for row in adopted if row["status"] == "claimed")["outbound_id"] == "om_claimed"
    for row in adopted:
        assert (
            row["worker_owner_key"],
            row["worker_id"],
            row["worker_generation"],
            row["lease_version"],
            row["recovery_generation"],
        ) == ("owner-a", "new-worker", 2, 3, 4)
    with pytest.raises(ValueError, match="complete collaboration delivery fence"):
        store.pending_origin_deliveries(worker_owner_key="owner-a", worker_id="partial")
    with pytest.raises(RuntimeError, match="owner mismatch"):
        store.pending_origin_deliveries(
            worker_owner_key="owner-b",
            worker_id="new-worker",
            worker_generation=2,
            lease_version=3,
            recovery_generation=4,
        )


def test_control_plane_handoff_enqueues_then_acks_without_connector_call(monkeypatch):
    from hermes_cli.owner_worker import collaboration_dispatcher

    owner = SimpleNamespace(owner_key="owner-a", owner_context=object())
    supervisor = SimpleNamespace(
        get_or_start=lambda _owner: SimpleNamespace(
            worker_id="worker-a",
            worker_generation=2,
            lease_version=3,
            recovery_generation=4,
        )
    )
    requests = []
    enqueued = []

    monkeypatch.setattr(
        collaboration_dispatcher,
        "_authenticated_owners",
        lambda *_args: (owner,),
    )

    def _request(_supervisor, _owner, path, *, content=None):
        requests.append((path, content))
        if path == "/internal/collaboration/deliveries":
            return {
                "deliveries": [
                    {
                        "delivery_key": "collaboration:task-a:creation",
                        "payload_text": "created",
                        "provider": "feishu",
                        "employee_id": "employee-a",
                        "connector_account_id": "ca-employee-a",
                        "binding_id": "binding-a",
                        "conversation_id": "oc_direct",
                        "thread_id": "",
                        "worker_owner_key": "owner-a",
                        "worker_id": "worker-a",
                        "worker_generation": 2,
                        "lease_version": 3,
                        "recovery_generation": 4,
                    }
                ]
            }
        return {"recorded": True}

    monkeypatch.setattr(collaboration_dispatcher, "_dispatch_owner_request", _request)
    count = collaboration_dispatcher.dispatch_owner_collaboration_deliveries(
        supervisor,
        "/global",
        authority_store=object(),
        enqueue_delivery=lambda **kwargs: enqueued.append(kwargs) or "om_stable",
    )

    assert count == 1
    assert enqueued == [
        {
            "owner_key": "owner-a",
            "account_id": "ca-employee-a",
            "binding_id": "binding-a",
            "conversation_id": "oc_direct",
            "thread_id": "",
            "delivery_key": "collaboration:task-a:creation",
            "payload": "created",
        }
    ]
    assert requests[1][0] == (
        "/internal/collaboration/delivery/collaboration:task-a:creation/ack"
    )
    assert json.loads(requests[1][1]) == {
        "outbound_id": "om_stable",
        "status": None,
        "error": None,
    }
    assert "Feishu" not in " ".join(str(item) for item in enqueued)


def test_control_plane_reconciles_terminal_canonical_outbox_state(monkeypatch):
    from hermes_cli.owner_worker import collaboration_dispatcher

    owner = SimpleNamespace(owner_key="owner-a", owner_context=object())
    supervisor = SimpleNamespace(
        get_or_start=lambda _owner: SimpleNamespace(
            worker_id="worker-a",
            worker_generation=2,
            lease_version=3,
            recovery_generation=4,
        )
    )
    requests = []
    monkeypatch.setattr(
        collaboration_dispatcher, "_authenticated_owners", lambda *_args: (owner,)
    )

    def _request(_supervisor, _owner, path, *, content=None):
        requests.append((path, content))
        if path == "/internal/collaboration/deliveries":
            return {
                "deliveries": [{
                    "delivery_key": "collaboration:task-a:completion",
                    "outbound_id": "om_stable",
                    "provider": "feishu",
                    "worker_owner_key": "owner-a",
                    "worker_id": "worker-a",
                    "worker_generation": 2,
                    "lease_version": 3,
                    "recovery_generation": 4,
                }]
            }
        return {"recorded": True}

    monkeypatch.setattr(collaboration_dispatcher, "_dispatch_owner_request", _request)
    count = collaboration_dispatcher.dispatch_owner_collaboration_deliveries(
        supervisor,
        "/global",
        authority_store=object(),
        enqueue_delivery=lambda **_kwargs: pytest.fail("must not enqueue again"),
        delivery_status=lambda outbound_id: {
            "status": "ambiguous",
            "error": f"uncertain:{outbound_id}",
        },
    )

    assert count == 1
    assert json.loads(requests[1][1]) == {
        "outbound_id": "om_stable",
        "status": "ambiguous",
        "error": "uncertain:om_stable",
    }


def test_feishu_origin_can_terminally_record_ambiguous_handoff(db):
    store = CollaborationStore(db, owner_key="owner-a")
    created, _ = store.create_ai_task(
        title="Ambiguous delivery",
        brief="Brief",
        creator=CollaborationMemberProfile("creator", 1, "fingerprint-creator-r1"),
        members=[CollaborationMemberProfile("employee-a", 1, "fingerprint-employee-a-r1")],
        source_kind="feishu_direct",
        source_provider="feishu",
        source_connector_account_id="creator",
        source_binding_id="binding-a",
        source_conversation_id="oc_direct",
        source_thread_id="",
        source_session_id="session-a",
        source_group_id=None,
        source_event_id=None,
        source_task_id=None,
        depth=1,
        allowed_attachment_ids=(),
        idempotency_key="ambiguous-delivery",
    )
    delivery = store.ensure_origin_delivery_intent(
        created["task_id"],
        completion=False,
        worker_owner_key="owner-a",
    )
    assert store.record_origin_delivery_result(
        delivery["delivery_key"],
        error="connection lost after handoff",
        ambiguous=True,
    ) is True
    with db._lock:
        row = db._conn.execute(
            "SELECT status, last_error FROM collaboration_delivery_state"
        ).fetchone()
    assert row["status"] == "ambiguous"
    assert row["last_error"] == "connection lost after handoff"
    assert store.pending_origin_deliveries() == []


def test_member_prompt_excludes_structured_owner_mentions(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Mention display",
        members=[
            CollaborationMemberProfile(
                "employee-a", 1, "fingerprint-employee-a-r1"
            )
        ],
    )
    membership = store.active_memberships(group.group_id)[0]
    store.submit_owner_message(
        group.group_id,
        text="Review this",
        mentioned_membership_ids=[membership.membership_id],
    )
    scheduler = CollaborationScheduler(
        db,
        store=store,
        resolver=_Resolver(),
        runner=_Runner(),
        runtime=_Runtime(),
        emit=lambda *_args: None,
    )
    claimed = scheduler._claim_next()
    prompt = scheduler._context_prompt(claimed)
    scheduler.close()

    owner_message_line = next(
        line for line in prompt.splitlines() if "owner message.owner" in line
    )
    assert owner_message_line.endswith('{"text": "Review this"}')


def test_ai_created_group_prompt_transfers_brief_not_source_transcript(db):
    source_secret = "SOURCE TRANSCRIPT MUST NOT TRANSFER"
    db.create_session("origin-session", "dashboard-gui")
    db.append_message("origin-session", "user", source_secret)
    store = CollaborationStore(db, owner_key="owner-a")
    created, _ = store.create_ai_task(
        title="Transcript isolation",
        brief="Only this explicit brief transfers",
        creator=CollaborationMemberProfile("creator", 1, "fingerprint-creator-r1"),
        members=[
            CollaborationMemberProfile(
                "employee-a", 1, "fingerprint-employee-a-r1"
            )
        ],
        source_kind="web_direct",
        source_conversation_id="origin-session",
        source_group_id=None,
        source_event_id=None,
        source_task_id=None,
        depth=1,
        allowed_attachment_ids=(),
        idempotency_key="transcript-isolation",
    )
    submitted, _, _ = store.dispatch_ai_round(
        created["task_id"],
        instruction="Only this explicit brief transfers",
        target_employee_ids=["employee-a"],
        attachment_ids=[],
        idempotency_key="transcript-isolation-round",
    )
    scheduler = CollaborationScheduler(
        db,
        store=store,
        resolver=_Resolver(may_create=True),
        runner=_Runner(),
        runtime=_Runtime(),
        emit=lambda *_args: None,
    )
    claimed = scheduler._claim_next()
    prompt = scheduler._context_prompt(claimed)
    scheduler.close()

    assert submitted.turn is not None
    assert "Only this explicit brief transfers" in prompt
    assert source_secret not in prompt


def test_ai_task_round_uses_member_context_then_exact_creator_coordinator(db):
    resolver = _Resolver(may_create=True)
    runner = _Runner()
    service = CollaborationService(
        db,
        owner_key="owner-a",
        resolver=resolver,
        emit=lambda *_args: None,
        ensure_member_session=lambda **_kwargs: None,
        deliver_web_origin=lambda **_kwargs: None,
    )
    runner.bind_service(service)
    store = service.store
    created, _ = store.create_ai_task(
        title="Pinned creator review",
        brief="Review without scheduling @text",
        creator=CollaborationMemberProfile("creator", 1, "fingerprint-creator-r1"),
        members=[CollaborationMemberProfile("employee-a", 1, "fingerprint-employee-a-r1")],
        source_kind="web_direct",
        source_conversation_id="origin-session",
        source_group_id=None,
        source_event_id=None,
        source_task_id=None,
        depth=1,
        allowed_attachment_ids=(),
        idempotency_key="create-pinned",
    )
    submitted, _, _ = store.dispatch_ai_round(
        created["task_id"],
        instruction="Review @creator literally",
        target_employee_ids=["creator"],
        attachment_ids=[],
        idempotency_key="round-pinned",
    )
    scheduler = CollaborationScheduler(
        db,
        store=store,
        resolver=resolver,
        runner=runner,
        runtime=_Runtime(),
        emit=lambda *_args: None,
        capacity=2,
        poll_seconds=0.02,
    )
    service.bind_scheduler(scheduler)
    try:
        scheduler.start()
        deadline = time.time() + 3
        while time.time() < deadline:
            if len(runner.calls) >= 2:
                break
            time.sleep(0.02)
    finally:
        scheduler.close()

    assert scheduler.turn_status(submitted.turn.turn_id)["status"] == "completed"
    assert len(runner.calls) == 2
    assert runner.calls[0]["collaboration_context"].role == "member"
    assert runner.calls[0]["collaboration_context"].source_depth == 1
    assert runner.calls[1]["collaboration_context"].role == "coordinator"
    assert runner.calls[1]["collaboration_context"].creator_employee_id == "creator"
    assert runner.calls[1]["stored_session_id"] == f"coordinator-{created['task_id']}"


def test_finish_summary_with_textual_mentions_does_not_schedule_origin_targets(db):
    resolver = _Resolver(may_create=True)
    delivered = []
    service = CollaborationService(
        db,
        owner_key="owner-a",
        resolver=resolver,
        emit=lambda *_args: None,
        ensure_member_session=lambda **_kwargs: None,
        deliver_web_origin=lambda **kwargs: delivered.append(kwargs),
    )
    source = service.store.create_group(
        "Source group",
        members=[
            CollaborationMemberProfile(
                "source-employee", 1, "fingerprint-source-employee-r1"
            )
        ],
    )
    created, _ = service.store.create_ai_task(
        title="Literal mention summary",
        brief="Review once",
        creator=CollaborationMemberProfile(
            "creator", 1, "fingerprint-creator-r1"
        ),
        members=[
            CollaborationMemberProfile(
                "employee-a", 1, "fingerprint-employee-a-r1"
            )
        ],
        source_kind="web_group",
        source_conversation_id=source.group_id,
        source_group_id=source.group_id,
        source_event_id=None,
        source_task_id=None,
        depth=1,
        allowed_attachment_ids=(),
        idempotency_key="create-literal-summary",
    )
    context = CollaborationAgentContext(
        service=service,
        creator_employee_id="creator",
        source_kind="web_group",
        source_conversation_id=source.group_id,
        source_group_id=source.group_id,
        source_depth=1,
        task_id=created["task_id"],
        role="coordinator",
    )

    result = service.finish_internal_group_task(
        context=context,
        summary="Completed; @source-employee is plain summary text.",
        idempotency_key="finish-literal-summary",
    )

    assert result["status"] == "completed"
    assert delivered[0]["completion"] is True
    assert "@source-employee" in delivered[0]["task"]["summary_text"]
    source_snapshot = service.store.snapshot_payload(source.group_id)
    assert source_snapshot["turns"] == []
    assert source_snapshot["targets"] == []


def test_creator_coordinator_plain_text_marks_task_and_receipt_ambiguous(db):
    resolver = _Resolver(may_create=True)
    runner = _Runner(text="I would finish without calling the tool")
    service = CollaborationService(
        db,
        owner_key="owner-a",
        resolver=resolver,
        emit=lambda *_args: None,
        ensure_member_session=lambda **_kwargs: None,
        deliver_web_origin=lambda **_kwargs: None,
    )
    runner.bind_service(service)
    created, _ = service.store.create_ai_task(
        title="Action required",
        brief="Return one result",
        creator=CollaborationMemberProfile("creator", 1, "fingerprint-creator-r1"),
        members=[
            CollaborationMemberProfile(
                "employee-a", 1, "fingerprint-employee-a-r1"
            )
        ],
        source_kind="web_direct",
        source_conversation_id="origin-session",
        source_group_id=None,
        source_event_id=None,
        source_task_id=None,
        depth=1,
        allowed_attachment_ids=(),
        idempotency_key="create-action-required",
    )
    submitted, _, _ = service.store.dispatch_ai_round(
        created["task_id"],
        instruction="Reply once",
        target_employee_ids=["employee-a"],
        attachment_ids=[],
        idempotency_key="dispatch-action-required",
    )
    scheduler = CollaborationScheduler(
        db,
        store=service.store,
        resolver=resolver,
        runner=runner,
        runtime=_Runtime(),
        emit=lambda *_args: None,
        capacity=2,
        poll_seconds=0.02,
    )
    service.bind_scheduler(scheduler)
    try:
        scheduler.start()
        deadline = time.time() + 3
        while time.time() < deadline:
            if service.store.ai_task(created["task_id"])["status"] == "ambiguous":
                break
            time.sleep(0.02)
    finally:
        scheduler.close()

    task = service.store.ai_task(created["task_id"])
    receipt_key = (
        f"collaboration-coordinator:{created['task_id']}:{submitted.turn.turn_id}"
    )
    with db._lock:
        receipt = db._conn.execute(
            "SELECT status, result_status FROM external_turn_receipts WHERE turn_key=?",
            (receipt_key,),
        ).fetchone()
        event = db._conn.execute(
            "SELECT body_json FROM collaboration_events WHERE group_id=? "
            "AND event_kind='task.ambiguous'",
            (created["group_id"],),
        ).fetchone()
    assert task["status"] == "ambiguous"
    assert tuple(receipt) == ("completed", "ambiguous")
    assert "must dispatch or finish exactly once" in event["body_json"]


def test_scheduler_claims_with_full_fence_and_appends_output_before_completion(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Runtime",
        members=[
            CollaborationMemberProfile(
                "employee-a", 1, "fingerprint-employee-a-r1"
            )
        ],
    )
    membership = store.active_memberships(group.group_id)[0]
    submitted = store.submit_owner_message(
        group.group_id,
        text="Please answer",
        mentioned_membership_ids=[membership.membership_id],
    )
    target = submitted.turn.targets[0]
    emitted = []
    scheduler = CollaborationScheduler(
        db,
        store=store,
        resolver=_Resolver(),
        runner=_Runner(text="reply with @literal"),
        runtime=_Runtime(),
        emit=lambda event, payload: emitted.append((event, payload)),
        capacity=1,
    )
    try:
        scheduler.start()
        deadline = time.time() + 3
        while time.time() < deadline:
            status = scheduler.turn_status(submitted.turn.turn_id)
            if status["status"] == "completed":
                break
            time.sleep(0.02)
        assert status["status"] == "completed"
    finally:
        scheduler.close()

    with db._lock:
        row = db._conn.execute(
            "SELECT status, worker_owner_key, worker_id, worker_generation, "
            "lease_version, recovery_generation FROM collaboration_turn_targets "
            "WHERE target_id=?",
            (target.target_id,),
        ).fetchone()
        event = db._conn.execute(
            "SELECT body_json FROM collaboration_events WHERE group_id=? "
            "AND event_kind='message.employee'",
            (group.group_id,),
        ).fetchone()
        target_count = db._conn.execute(
            "SELECT COUNT(*) FROM collaboration_turn_targets"
        ).fetchone()[0]
    assert tuple(row) == ("completed", "owner-a", "worker-a", 2, 3, 4)
    assert "@literal" in event["body_json"]
    assert target_count == 1
    event_index = next(i for i, item in enumerate(emitted) if item[0] == "collaboration.event.appended")
    complete_index = next(
        i
        for i, item in enumerate(emitted)
        if item[0] == "collaboration.target.changed" and item[1]["status"] == "completed"
    )
    assert event_index < complete_index


def test_scheduler_runs_three_rounds_and_emits_round_events(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Three rounds",
        members=[CollaborationMemberProfile("employee-a", 1, "fingerprint-employee-a-r1")],
    )
    membership = store.active_memberships(group.group_id)[0]
    submitted = store.submit_owner_message(
        group.group_id,
        text="Discuss this for 3 rounds",
        total_rounds=3,
        mentioned_membership_ids=[membership.membership_id],
    )
    emitted = []
    runner = _Runner()
    scheduler = CollaborationScheduler(
        db,
        store=store,
        resolver=_Resolver(),
        runner=runner,
        runtime=_Runtime(),
        emit=lambda event, payload: emitted.append((event, payload)),
        capacity=1,
        poll_seconds=0.02,
    )
    try:
        scheduler.start()
        deadline = time.time() + 5
        while time.time() < deadline:
            with db._lock:
                discussion = db._conn.execute(
                    "SELECT d.status, MAX(r.round_number) "
                    "FROM collaboration_discussions d "
                    "JOIN collaboration_discussion_rounds r "
                    "ON r.discussion_id=d.discussion_id WHERE d.owner_event_id=? "
                    "GROUP BY d.discussion_id, d.status",
                    (submitted.event.event_id,),
                ).fetchone()
            if tuple(discussion) == ("completed", 3):
                break
            time.sleep(0.02)
    finally:
        scheduler.close()

    assert tuple(discussion) == ("completed", 3)
    assert len(runner.calls) == 3
    assert "discussion round 2" in runner.calls[1]["prompt"]
    assert "employee reply" in runner.calls[1]["prompt"]
    round_events = [
        payload
        for event, payload in emitted
        if event == "collaboration.event.appended"
        and payload["event_kind"] == "discussion.round.started"
    ]
    assert [event["body"]["discussion_round"] for event in round_events] == [2, 3]
    assert all(event["body"]["total_rounds"] == 3 for event in round_events)


def test_completion_crash_rolls_back_receipt_target_and_event_then_replays_once(
    db, monkeypatch
):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Atomic completion",
        members=[CollaborationMemberProfile("employee-a", 1, "fingerprint-employee-a-r1")],
    )
    membership = store.active_memberships(group.group_id)[0]
    submitted = store.submit_owner_message(
        group.group_id,
        text="Complete once",
        mentioned_membership_ids=[membership.membership_id],
    )
    scheduler = CollaborationScheduler(
        db,
        store=store,
        resolver=_Resolver(),
        runner=_Runner(),
        runtime=_Runtime(),
        emit=lambda *_args: None,
    )
    claimed = scheduler._claim_next()
    receipt_key = f"collaboration:{claimed['execution_id']}"
    db.begin_external_turn(
        turn_key=receipt_key,
        stored_session_id=claimed["stored_session_id"],
        worker_id="worker-a",
        worker_generation=2,
    )
    append_event = store._append_event

    def _crash(*_args, **_kwargs):
        raise RuntimeError("injected completion crash")

    monkeypatch.setattr(store, "_append_event", _crash)
    with pytest.raises(RuntimeError, match="injected completion crash"):
        scheduler._commit_completed(
            claimed["target_id"],
            receipt_key=receipt_key,
            text="one reply",
            result_status="complete",
        )
    with db._lock:
        target_status = db._conn.execute(
            "SELECT status FROM collaboration_turn_targets WHERE target_id=?",
            (claimed["target_id"],),
        ).fetchone()[0]
        receipt_status = db._conn.execute(
            "SELECT status FROM external_turn_receipts WHERE turn_key=?",
            (receipt_key,),
        ).fetchone()[0]
        event_count = db._conn.execute(
            "SELECT COUNT(*) FROM collaboration_events WHERE group_id=? "
            "AND event_kind='message.employee'",
            (group.group_id,),
        ).fetchone()[0]
    assert (target_status, receipt_status, event_count) == ("running", "processing", 0)

    monkeypatch.setattr(store, "_append_event", append_event)
    scheduler._commit_completed(
        claimed["target_id"],
        receipt_key=receipt_key,
        text="one reply",
        result_status="complete",
    )
    scheduler._commit_completed(
        claimed["target_id"],
        receipt_key=receipt_key,
        text="ignored replay",
        result_status="complete",
    )
    with db._lock:
        event_count = db._conn.execute(
            "SELECT COUNT(*) FROM collaboration_events WHERE group_id=? "
            "AND event_kind='message.employee'",
            (group.group_id,),
        ).fetchone()[0]
        receipt = db._conn.execute(
            "SELECT status, collaboration_target_id, collaboration_event_id "
            "FROM external_turn_receipts WHERE turn_key=?",
            (receipt_key,),
        ).fetchone()
    scheduler.close()
    assert event_count == 1
    assert receipt["status"] == "completed"
    assert receipt["collaboration_target_id"] == claimed["target_id"]
    assert receipt["collaboration_event_id"]


def test_archive_cancels_work_expires_approval_and_blocks_stale_completion(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Archive",
        members=[CollaborationMemberProfile("employee-a", 1, "fingerprint-employee-a-r1")],
    )
    membership = store.active_memberships(group.group_id)[0]
    running = store.submit_owner_message(
        group.group_id,
        text="Running",
        mentioned_membership_ids=[membership.membership_id],
    )
    queued = store.submit_owner_message(
        group.group_id,
        text="Queued",
        mentioned_membership_ids=[membership.membership_id],
    )
    scheduler = CollaborationScheduler(
        db,
        store=store,
        resolver=_Resolver(),
        runner=_Runner(),
        runtime=_Runtime(),
        emit=lambda *_args: None,
        capacity=1,
    )
    claimed = scheduler._claim_next()
    assert claimed is not None
    now = time.time()
    with db._lock:
        db._conn.execute(
            "UPDATE collaboration_turn_targets SET status='waiting_approval', "
            "active_started_at=NULL WHERE target_id=?",
            (claimed["target_id"],),
        )
        db._conn.execute(
            "INSERT INTO collaboration_approvals "
            "(approval_id, target_id, tool_call_id, tool_name, status, "
            "worker_owner_key, worker_id, worker_generation, lease_version, "
            "recovery_generation, created_at, updated_at) VALUES "
            "('archive-approval', ?, 'tool-call-a', 'terminal', 'pending', "
            "'owner-a', 'worker-a', 2, 3, 4, ?, ?)",
            (claimed["target_id"], now, now),
        )
        db._conn.commit()

    archived, live_sessions = store.archive_group(group.group_id)
    assert archived.status == "archived"
    assert live_sessions == (membership.hidden_session_id,)
    with db._lock:
        target_statuses = {
            row["turn_id"]: row["status"]
            for row in db._conn.execute(
                "SELECT turn_id, status FROM collaboration_turn_targets"
            ).fetchall()
        }
        turn_statuses = {
            row["turn_id"]: row["status"]
            for row in db._conn.execute(
                "SELECT turn_id, status FROM collaboration_turns"
            ).fetchall()
        }
        approval_status = db._conn.execute(
            "SELECT status FROM collaboration_approvals WHERE approval_id='archive-approval'"
        ).fetchone()[0]
    assert set(target_statuses.values()) == {"cancelled"}
    assert turn_statuses == {
        running.turn.turn_id: "cancelled",
        queued.turn.turn_id: "cancelled",
    }
    assert approval_status == "expired"

    receipt_key = f"collaboration:{claimed['execution_id']}"
    db.begin_external_turn(
        turn_key=receipt_key,
        stored_session_id=membership.stored_session_id,
        worker_id="worker-a",
        worker_generation=2,
    )
    with pytest.raises(RuntimeError, match="archived"):
        scheduler._commit_completed(
            claimed["target_id"],
            receipt_key=receipt_key,
            text="stale output",
            result_status="complete",
        )
    with db._lock:
        employee_events = db._conn.execute(
            "SELECT COUNT(*) FROM collaboration_events WHERE group_id=? "
            "AND event_kind='message.employee'",
            (group.group_id,),
        ).fetchone()[0]
    scheduler.close()
    assert employee_events == 0


def test_service_archive_best_effort_interrupts_live_sessions(db):
    service = CollaborationService(
        db,
        owner_key="owner-a",
        resolver=_Resolver(),
        emit=lambda *_args: None,
        ensure_member_session=lambda **_kwargs: None,
    )
    created = service.create_group(
        name="Interrupt",
        employee_ids=["employee-a"],
        client_idempotency_key="archive-interrupt",
    )
    membership = service.store.active_memberships(created["group"]["group_id"])[0]
    service.store.submit_owner_message(
        created["group"]["group_id"],
        text="Running",
        mentioned_membership_ids=[membership.membership_id],
    )
    interrupted = []
    scheduler = SimpleNamespace(
        interrupt_session=lambda hidden_id: interrupted.append(hidden_id) or True,
        wake=lambda: None,
    )
    service.bind_scheduler(scheduler)
    with db._lock:
        db._conn.execute(
            "UPDATE collaboration_turn_targets SET status='running'"
        )
        db._conn.commit()

    service.archive_group(created["group"]["group_id"])

    assert interrupted == [membership.hidden_session_id]


def test_stale_completion_fence_cannot_append_employee_event(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Fenced",
        members=[CollaborationMemberProfile("employee-a", 1, "fingerprint-employee-a-r1")],
    )
    membership = store.active_memberships(group.group_id)[0]
    submitted = store.submit_owner_message(
        group.group_id,
        text="Fence this",
        mentioned_membership_ids=[membership.membership_id],
    )
    runner = _Runner()
    scheduler = CollaborationScheduler(
        db,
        store=store,
        resolver=_Resolver(),
        runner=runner,
        runtime=_Runtime(),
        emit=lambda *_args: None,
        capacity=1,
    )
    claimed = scheduler._claim_next()
    assert claimed is not None
    with db._lock:
        db._conn.execute(
            "UPDATE collaboration_turn_targets SET worker_id='replacement' WHERE target_id=?",
            (claimed["target_id"],),
        )
        db._conn.commit()

    scheduler._finish(claimed["target_id"], status="completed", text="stale output")

    with db._lock:
        target = db._conn.execute(
            "SELECT status, result_json FROM collaboration_turn_targets WHERE target_id=?",
            (claimed["target_id"],),
        ).fetchone()
        employee_events = db._conn.execute(
            "SELECT COUNT(*) FROM collaboration_events WHERE group_id=? "
            "AND event_kind='message.employee'",
            (group.group_id,),
        ).fetchone()[0]
    scheduler.close()
    assert target["status"] == "running"
    assert target["result_json"] is None
    assert employee_events == 0


def test_scheduler_serializes_and_coalesces_queued_messages_for_one_member(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Serialized",
        members=[CollaborationMemberProfile("employee-a", 1, "fingerprint-employee-a-r1")],
    )
    membership = store.active_memberships(group.group_id)[0]
    first = store.submit_owner_message(
        group.group_id,
        text="First",
        mentioned_membership_ids=[membership.membership_id],
    )
    block = threading.Event()
    runner = _Runner(block=block)
    scheduler = CollaborationScheduler(
        db,
        store=store,
        resolver=_Resolver(),
        runner=runner,
        runtime=_Runtime(),
        emit=lambda *_args: None,
        capacity=2,
        poll_seconds=0.02,
    )
    try:
        scheduler.start()
        assert runner.started.wait(timeout=2)
        second = store.submit_owner_message(
            group.group_id,
            text="Second",
            mentioned_membership_ids=[membership.membership_id],
        )
        third = store.submit_owner_message(
            group.group_id,
            text="Third",
            mentioned_membership_ids=[membership.membership_id],
        )
        scheduler.wake()
        time.sleep(0.1)
        with db._lock:
            statuses = [
                row["status"]
                for row in db._conn.execute(
                    "SELECT status FROM collaboration_turn_targets "
                    "ORDER BY snapshot_sequence"
                ).fetchall()
            ]
        assert statuses == ["running", "queued", "queued"]

        block.set()
        deadline = time.time() + 3
        while time.time() < deadline:
            if scheduler.turn_status(third.turn.turn_id)["status"] == "completed":
                break
            time.sleep(0.02)
        assert scheduler.turn_status(first.turn.turn_id)["status"] == "completed"
        assert scheduler.turn_status(second.turn.turn_id)["status"] == "completed"
        assert scheduler.turn_status(third.turn.turn_id)["status"] == "completed"
    finally:
        block.set()
        scheduler.close()

    assert len(runner.calls) == 2
    merged_prompt = runner.calls[1]["prompt"]
    assert merged_prompt.index('"text": "Second"') < merged_prompt.index(
        '"text": "Third"'
    )
    with db._lock:
        owner_events = db._conn.execute(
            "SELECT COUNT(*) FROM collaboration_events WHERE group_id=? "
            "AND event_kind='message.owner'",
            (group.group_id,),
        ).fetchone()[0]
        employee_events = db._conn.execute(
            "SELECT event_id, body_json FROM collaboration_events WHERE group_id=? "
            "AND event_kind='message.employee' ORDER BY sequence",
            (group.group_id,),
        ).fetchall()
        folded = {
            row["target_id"]: json.loads(row["result_json"])
            for row in db._conn.execute(
                "SELECT target_id, result_json FROM collaboration_turn_targets "
                "WHERE target_id IN (?, ?)",
                (second.turn.targets[0].target_id, third.turn.targets[0].target_id),
            ).fetchall()
        }
    assert owner_events == 3
    assert len(employee_events) == 2
    merged_body = json.loads(employee_events[1]["body_json"])
    assert merged_body["coalesced_target_ids"] == [third.turn.targets[0].target_id]
    second_result = folded[second.turn.targets[0].target_id]
    third_result = folded[third.turn.targets[0].target_id]
    assert (
        second_result["event_id"]
        == third_result["event_id"]
        == employee_events[1]["event_id"]
    )
    assert second_result["status"] == "complete"
    assert third_result == {
        "text": "employee reply",
        "status": "coalesced",
        "event_id": employee_events[1]["event_id"],
        "coalesced_into_target_id": second.turn.targets[0].target_id,
    }


def test_interrupted_coalesced_follower_interrupts_the_shared_member_session(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Interrupt batch",
        members=[CollaborationMemberProfile("employee-a", 1, "fingerprint-employee-a-r1")],
    )
    membership = store.active_memberships(group.group_id)[0]
    first = store.submit_owner_message(
        group.group_id,
        text="First",
        mentioned_membership_ids=[membership.membership_id],
    )
    second = store.submit_owner_message(
        group.group_id,
        text="Second",
        mentioned_membership_ids=[membership.membership_id],
    )
    runner = _Runner()
    scheduler = CollaborationScheduler(
        db,
        store=store,
        resolver=_Resolver(),
        runner=runner,
        runtime=_Runtime(),
        emit=lambda *_args: None,
    )
    claimed = scheduler._claim_next()
    merged = scheduler._coalesce_queued_targets(claimed)
    assert [target["target_id"] for target in merged] == [
        second.turn.targets[0].target_id
    ]

    interrupted = scheduler.interrupt(second.turn.targets[0].target_id)

    assert interrupted["status"] == "cancelled"
    assert runner.interrupted == [membership.hidden_session_id]
    assert scheduler.turn_status(first.turn.turn_id)["status"] == "running"
    scheduler.close()


def test_scheduler_stops_ordinary_coalescing_before_a_discussion_round(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Discussion follower boundary",
        members=[CollaborationMemberProfile("employee-a", 1, "fingerprint-employee-a-r1")],
    )
    membership = store.active_memberships(group.group_id)[0]
    ordinary = store.submit_owner_message(
        group.group_id,
        text="Ordinary",
        mentioned_membership_ids=[membership.membership_id],
    )
    store.submit_owner_message(
        group.group_id,
        text="Discuss for 2 rounds",
        total_rounds=2,
        mentioned_membership_ids=[membership.membership_id],
    )
    scheduler = CollaborationScheduler(
        db,
        store=store,
        resolver=_Resolver(),
        runner=_Runner(),
        runtime=_Runtime(),
        emit=lambda *_args: None,
    )
    claimed = scheduler._claim_next()
    assert claimed["target_id"] == ordinary.turn.targets[0].target_id
    assert scheduler._coalesce_queued_targets(claimed) == []
    scheduler.close()


def test_scheduler_treats_discussion_round_as_a_coalescing_barrier(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Discussion boundary",
        members=[CollaborationMemberProfile("employee-a", 1, "fingerprint-employee-a-r1")],
    )
    membership = store.active_memberships(group.group_id)[0]
    first = store.submit_owner_message(
        group.group_id,
        text="Discuss for 2 rounds",
        total_rounds=2,
        mentioned_membership_ids=[membership.membership_id],
    )
    store.submit_owner_message(
        group.group_id,
        text="Unrelated follow-up",
        mentioned_membership_ids=[membership.membership_id],
    )
    scheduler = CollaborationScheduler(
        db,
        store=store,
        resolver=_Resolver(),
        runner=_Runner(),
        runtime=_Runtime(),
        emit=lambda *_args: None,
    )
    claimed = scheduler._claim_next()
    assert claimed["target_id"] == first.turn.targets[0].target_id
    assert scheduler._coalesce_queued_targets(claimed) == []
    scheduler.close()


def test_scheduler_stops_coalescing_at_an_attachment_target(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Attachment boundary",
        members=[CollaborationMemberProfile("employee-a", 1, "fingerprint-employee-a-r1")],
    )
    membership = store.active_memberships(group.group_id)[0]
    first = store.submit_owner_message(
        group.group_id,
        text="First",
        mentioned_membership_ids=[membership.membership_id],
    )
    attachment = store.create_attachment(
        group.group_id,
        filename="source.txt",
        media_type="text/plain",
        size_bytes=6,
        storage_key=f"collaboration/{group.group_id}/source.txt",
        content_sha256="a" * 64,
    )
    attached = store.submit_owner_message(
        group.group_id,
        text="Attached",
        mentioned_membership_ids=[membership.membership_id],
        attachment_ids=[attachment["attachment_id"]],
    )
    later = store.submit_owner_message(
        group.group_id,
        text="Later",
        mentioned_membership_ids=[membership.membership_id],
    )
    scheduler = CollaborationScheduler(
        db,
        store=store,
        resolver=_Resolver(),
        runner=_Runner(),
        runtime=_Runtime(),
        emit=lambda *_args: None,
    )
    claimed = scheduler._claim_next()
    assert claimed["target_id"] == first.turn.targets[0].target_id
    assert scheduler._coalesce_queued_targets(claimed) == []
    with db._lock:
        snapshots = {
            row["target_id"]: row["snapshot_sequence"]
            for row in db._conn.execute(
                "SELECT target_id, snapshot_sequence FROM collaboration_turn_targets"
            ).fetchall()
        }
    scheduler.close()
    assert snapshots[first.turn.targets[0].target_id] == first.event.sequence
    assert snapshots[attached.turn.targets[0].target_id] == attached.event.sequence
    assert snapshots[later.turn.targets[0].target_id] == later.event.sequence


def test_recovery_marks_every_running_coalesced_target_ambiguous(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Recover batch",
        members=[CollaborationMemberProfile("employee-a", 1, "fingerprint-employee-a-r1")],
    )
    membership = store.active_memberships(group.group_id)[0]
    first = store.submit_owner_message(
        group.group_id,
        text="First",
        mentioned_membership_ids=[membership.membership_id],
    )
    second = store.submit_owner_message(
        group.group_id,
        text="Second",
        mentioned_membership_ids=[membership.membership_id],
    )
    scheduler = CollaborationScheduler(
        db,
        store=store,
        resolver=_Resolver(),
        runner=_Runner(),
        runtime=_Runtime(),
        emit=lambda *_args: None,
    )
    claimed = scheduler._claim_next()
    scheduler._coalesce_queued_targets(claimed)
    scheduler.close()
    with db._lock:
        db._conn.execute(
            "UPDATE collaboration_turn_targets SET worker_id='previous-worker' "
            "WHERE target_id IN (?, ?)",
            (first.turn.targets[0].target_id, second.turn.targets[0].target_id),
        )
        db._conn.commit()

    recovered = CollaborationScheduler(
        db,
        store=store,
        resolver=_Resolver(),
        runner=_Runner(),
        runtime=_Runtime(),
        emit=lambda *_args: None,
    )
    try:
        recovered.start()
        assert recovered.turn_status(first.turn.turn_id)["status"] == "ambiguous"
        assert recovered.turn_status(second.turn.turn_id)["status"] == "ambiguous"
    finally:
        recovered.close()


def test_granted_attachment_materializes_under_membership_readonly_prefix(
    db, tmp_path, monkeypatch
):
    from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext
    from hermes_cli.collaboration.resolver import collaboration_member_policy
    from hermes_cli.controlled_roots import controlled_roots_for
    from hermes_cli.owner_runtime import ensure_owner_runtime_dirs, owner_worker_runtime_paths

    import hermes_cli.controlled_roots as controlled_roots

    monkeypatch.setattr(controlled_roots.sys, "platform", "linux")
    monkeypatch.setattr(controlled_roots, "_openat2", lambda *_args: None)
    paths = owner_worker_runtime_paths(
        owner_home=ensure_owner_runtime_dirs(tmp_path / "owner"),
        worker_generation=2,
    )
    paths.default_workspace.mkdir(parents=True, exist_ok=True)
    roots = controlled_roots_for(paths)
    context = AuthenticatedWorkspaceContext(roots)
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Attachments",
        members=[CollaborationMemberProfile("employee-a", 1, "fingerprint-employee-a-r1")],
    )
    membership = store.active_memberships(group.group_id)[0]
    storage_key = f"collaboration/{group.group_id}/source.txt"
    roots.replace_bytes(controlled_roots.RootKind.OWNER_WRITABLE, storage_key, b"secret")
    attachment = store.create_attachment(
        group.group_id,
        filename="source.txt",
        media_type="text/plain",
        size_bytes=6,
        storage_key=storage_key,
        content_sha256="a" * 64,
    )
    submitted = store.submit_owner_message(
        group.group_id,
        text="Read this",
        mentioned_membership_ids=[membership.membership_id],
        attachment_ids=[attachment["attachment_id"]],
    )
    runtime = SimpleNamespace(
        owner_key="owner-a",
        worker_generation=2,
        worker_id="worker-a",
        lease_version=3,
        recovery_generation=4,
        filesystem_context=context,
    )
    scheduler = CollaborationScheduler(
        db,
        store=store,
        resolver=_Resolver(),
        runner=_Runner(),
        runtime=runtime,
        emit=lambda *_args: None,
    )
    claimed = scheduler._claim_next()
    claimed["employee_policy"] = collaboration_member_policy(
        _policy("employee-a"), membership.membership_id
    )
    prompt = scheduler._context_prompt(claimed)
    reference = (
        f"/knowledge/0/{submitted.turn.targets[0].target_id}/0.txt"
    )
    assert reference in prompt
    with db._lock:
        materialized = db._conn.execute(
            "SELECT status, materialized_path FROM collaboration_attachment_materializations"
        ).fetchone()
    assert materialized["status"] == "completed"
    assert membership.membership_id in materialized["materialized_path"]
    scheduler.close()
    roots.close()


def test_waiting_approval_pauses_active_budget_and_resumes_exact_callback(db):
    from tools.approval import (
        _ApprovalEntry,
        register_gateway_notify,
        unregister_gateway_notify,
        _gateway_queues,
    )

    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Approval",
        members=[CollaborationMemberProfile("employee-a", 1, "fingerprint-employee-a-r1")],
    )
    membership = store.active_memberships(group.group_id)[0]
    submitted = store.submit_owner_message(
        group.group_id,
        text="Approve",
        mentioned_membership_ids=[membership.membership_id],
    )
    runner = _ApprovalRunner()
    emitted = []
    scheduler = CollaborationScheduler(
        db,
        store=store,
        resolver=_Resolver(),
        runner=runner,
        runtime=_Runtime(),
        emit=lambda event, payload: emitted.append((event, payload)),
        active_budget_seconds=1,
        poll_seconds=0.02,
    )
    register_gateway_notify(membership.stored_session_id, lambda _data: None)
    try:
        scheduler.start()
        assert runner.waiting.wait(timeout=2)
        with db._lock:
            approval = db._conn.execute(
                "SELECT approval_id, tool_call_id FROM collaboration_approvals"
            ).fetchone()
            entry = _ApprovalEntry({"tool_call_id": approval["tool_call_id"]})
            _gateway_queues.setdefault(membership.stored_session_id, []).append(entry)
            before = db._conn.execute(
                "SELECT active_seconds FROM collaboration_turn_targets"
            ).fetchone()[0]
        time.sleep(1.1)
        with db._lock:
            waiting = db._conn.execute(
                "SELECT status, active_seconds FROM collaboration_turn_targets"
            ).fetchone()
        assert waiting["status"] == "waiting_approval"
        assert waiting["active_seconds"] == pytest.approx(before)

        scheduler.respond_approval(approval["approval_id"], "once")
        assert entry.event.wait(timeout=1)
        assert entry.result == "once"
        runner.resume.set()
        deadline = time.time() + 2
        while time.time() < deadline:
            status = scheduler.turn_status(submitted.turn.turn_id)
            if status["status"] == "completed":
                break
            time.sleep(0.02)
        assert status["status"] == "completed"
        target_events = [
            payload
            for event, payload in emitted
            if event == "collaboration.target.changed"
        ]
        assert all(payload["group_id"] == group.group_id for payload in target_events)
    finally:
        runner.resume.set()
        scheduler.close()
        unregister_gateway_notify(membership.stored_session_id)


def test_scheduler_marks_prior_worker_and_pending_approval_ambiguous(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Recovery",
        members=[CollaborationMemberProfile("employee-a", 1, "fingerprint-employee-a-r1")],
    )
    membership = store.active_memberships(group.group_id)[0]
    submitted = store.submit_owner_message(
        group.group_id,
        text="Recover",
        mentioned_membership_ids=[membership.membership_id],
    )
    target = submitted.turn.targets[0]
    now = time.time()
    with db._lock:
        db._conn.execute(
            "UPDATE collaboration_turn_targets SET status='waiting_approval', "
            "worker_owner_key='owner-a', worker_id='old', worker_generation=1, "
            "lease_version=1, recovery_generation=0, active_started_at=NULL "
            "WHERE target_id=?",
            (target.target_id,),
        )
        db._conn.execute(
            "INSERT INTO collaboration_approvals "
            "(approval_id, target_id, tool_call_id, tool_name, status, "
            "worker_owner_key, worker_id, worker_generation, lease_version, "
            "recovery_generation, created_at, updated_at) VALUES "
            "('approval-a', ?, 'tool-call-a', 'terminal', 'pending', "
            "'owner-a', 'old', 1, 1, 0, ?, ?)",
            (target.target_id, now, now),
        )
        db._conn.commit()
    emitted = []
    scheduler = CollaborationScheduler(
        db,
        store=store,
        resolver=_Resolver(),
        runner=_Runner(),
        runtime=_Runtime(),
        emit=lambda event, payload: emitted.append((event, payload)),
    )
    try:
        scheduler.start()
    finally:
        scheduler.close()
    with db._lock:
        target_status = db._conn.execute(
            "SELECT status FROM collaboration_turn_targets WHERE target_id=?",
            (target.target_id,),
        ).fetchone()[0]
        approval_status = db._conn.execute(
            "SELECT status FROM collaboration_approvals WHERE approval_id='approval-a'"
        ).fetchone()[0]
    assert target_status == "ambiguous"
    assert approval_status == "ambiguous"
    target_events = [
        payload
        for event, payload in emitted
        if event == "collaboration.target.changed"
    ]
    assert len(target_events) == 1
    assert {
        key: target_events[0][key]
        for key in ("group_id", "turn_id", "target_id", "execution_id", "status")
    } == {
        "group_id": group.group_id,
        "turn_id": submitted.turn.turn_id,
        "target_id": target.target_id,
        "execution_id": target.execution_id,
        "status": "ambiguous",
    }
    approval_events = [
        payload
        for event, payload in emitted
        if event == "collaboration.approval.changed"
    ]
    assert approval_events == [
        {
            "approval_id": "approval-a",
            "group_id": group.group_id,
            "turn_id": submitted.turn.turn_id,
            "target_id": target.target_id,
            "execution_id": target.execution_id,
            "status": "ambiguous",
        }
    ]


def test_lost_approval_callback_marks_and_emits_ambiguous_state(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Lost approval",
        members=[CollaborationMemberProfile("employee-a", 1, "fingerprint-employee-a-r1")],
    )
    membership = store.active_memberships(group.group_id)[0]
    submitted = store.submit_owner_message(
        group.group_id,
        text="Approve",
        mentioned_membership_ids=[membership.membership_id],
    )
    emitted = []
    scheduler = CollaborationScheduler(
        db,
        store=store,
        resolver=_Resolver(),
        runner=_Runner(),
        runtime=_Runtime(),
        emit=lambda event, payload: emitted.append((event, payload)),
    )
    claimed = scheduler._claim_next()
    approval_id = scheduler.request_approval(
        target_id=claimed["target_id"],
        tool_call_id="lost-tool-call",
        tool_name="terminal",
        request={"description": "run safe command"},
    )
    emitted.clear()

    with pytest.raises(RuntimeError, match="callback is no longer live"):
        scheduler.respond_approval(approval_id, "once")

    with db._lock:
        target_status = db._conn.execute(
            "SELECT status FROM collaboration_turn_targets WHERE target_id=?",
            (claimed["target_id"],),
        ).fetchone()[0]
        approval_status = db._conn.execute(
            "SELECT status FROM collaboration_approvals WHERE approval_id=?",
            (approval_id,),
        ).fetchone()[0]
    assert target_status == "ambiguous"
    assert approval_status == "ambiguous"
    assert [event for event, _payload in emitted] == [
        "collaboration.approval.changed",
        "collaboration.target.changed",
    ]
    approval_payload = emitted[0][1]
    assert approval_payload == {
        "approval_id": approval_id,
        "group_id": group.group_id,
        "turn_id": submitted.turn.turn_id,
        "target_id": claimed["target_id"],
        "execution_id": claimed["execution_id"],
        "status": "ambiguous",
    }
    assert emitted[1][1]["status"] == "ambiguous"
    assert emitted[1][1]["group_id"] == group.group_id
    scheduler.close()


def test_scheduler_rejects_revoked_pinned_profile_before_execution(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Revoked",
        members=[CollaborationMemberProfile("employee-a", 1, "fingerprint-employee-a-r1")],
    )
    membership = store.active_memberships(group.group_id)[0]
    submitted = store.submit_owner_message(
        group.group_id,
        text="Do not run",
        mentioned_membership_ids=[membership.membership_id],
    )
    runner = _Runner()
    scheduler = CollaborationScheduler(
        db,
        store=store,
        resolver=_Resolver(revision=2, allowed=False),
        runner=runner,
        runtime=_Runtime(),
        emit=lambda *_args: None,
        capacity=1,
    )
    try:
        scheduler.start()
        deadline = time.time() + 2
        while time.time() < deadline:
            status = scheduler.turn_status(submitted.turn.turn_id)
            if status["status"] == "failed":
                break
            time.sleep(0.02)
    finally:
        scheduler.close()
    assert status["status"] == "failed"
    assert not runner.started.is_set()
