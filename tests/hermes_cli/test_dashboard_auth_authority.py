"""Unit tests for the Control Plane authority/replay store."""
from __future__ import annotations

import sqlite3
import threading

import pytest

from hermes_cli.dashboard_auth.authority import (
    AuthorityStore,
    AuthorityUnavailable,
    AuthorizationRejected,
    AuthorizationScope,
    WorkerGenerationState,
    WorkerLeaseState,
)


def _scope(
    *,
    user_id: str = "user-a",
    tenant_id: str = "tenant-a",
    session_id: str = "session-a",
    membership_revision: str = "member-v1",
) -> AuthorizationScope:
    return AuthorizationScope(
        provider="stub",
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session_id,
        membership_revision=membership_revision,
    )


def _consume(store: AuthorityStore, scope: AuthorizationScope, *, jti: str = "ticket-1"):
    state = store.activate(scope)
    return store.check_and_consume(
        scope,
        token_class="browser-ws",
        issuer_key_version="bwt1",
        jti=jti,
        audience="browser-ws:/api/ws",
        expires_at=1_000,
        claim_epoch=state.epoch,
        claim_recovery_generation=state.recovery_generation,
        now=999,
    )


def test_control_store_is_persistent_and_owner_scopes_are_isolated(tmp_path):
    store_a = AuthorityStore(tmp_path / "control-plane")
    scope_a = _scope()
    scope_b = _scope(user_id="user-b", session_id="session-b")

    assert store_a.activate(scope_a).epoch == 0
    assert store_a.activate(scope_b).epoch == 0
    store_a.revoke_and_bump(scope_a, reason="logout")

    with pytest.raises(AuthorizationRejected, match="session_revoked"):
        _consume(store_a, scope_a)
    assert _consume(store_a, scope_b).accepted is True

    restarted = AuthorityStore(tmp_path / "control-plane")
    with pytest.raises(AuthorizationRejected, match="session_revoked"):
        restarted.read_state(scope_a)
    assert restarted.read_state(scope_b).epoch == 0


def test_revoke_marks_session_and_bumps_epoch(tmp_path):
    store = AuthorityStore(tmp_path / "control-plane")
    scope = _scope()
    state = store.activate(scope)
    assert state.epoch == 0

    revoked = store.revoke_and_bump(scope, reason="logout")
    assert revoked.epoch == 1

    with pytest.raises(AuthorizationRejected, match="session_revoked"):
        store.read_state(scope)


def test_principal_revocation_fences_all_active_scopes_for_one_user(tmp_path):
    store = AuthorityStore(tmp_path / "control-plane")
    first = _scope(user_id="local-account", tenant_id="tenant-a", session_id="one")
    second = _scope(user_id="local-account", tenant_id="tenant-b", session_id="two")
    other = _scope(user_id="other-account", tenant_id="tenant-a", session_id="three")

    # The normal activation path supersedes another active session for a
    # principal, so seed distinct tenant/session variants directly to model
    # scopes admitted by concurrently running dashboard workers.
    store.activate(first)
    with store._connect() as conn:  # noqa: SLF001 - setup for principal-wide test
        conn.execute(
            "INSERT INTO authorization_scopes(scope_digest, principal_digest, membership_revision, epoch, revoked) "
            "VALUES (?, ?, ?, ?, 0)",
            (second.digest, second.principal_digest, second.membership_revision, 3),
        )
    store.activate(other)

    revoked = store.revoke_principal_and_bump(
        provider="stub", user_id="local-account", reason="password_reset"
    )

    assert set(revoked.revoked_scope_digests) == {first.digest, second.digest}
    assert {change.scope_digest for change in revoked.changes} == {first.digest, second.digest}
    assert all(change.revoked for change in revoked.changes)
    assert revoked.epoch == 4
    with pytest.raises(AuthorizationRejected, match="session_revoked"):
        store.read_state(first)
    with pytest.raises(AuthorizationRejected, match="session_revoked"):
        store.read_state(second)
    assert store.read_state(other).epoch == 0


def test_revoke_rejects_stale_membership_revision(tmp_path):
    store = AuthorityStore(tmp_path / "control-plane")
    original = _scope(membership_revision="member-v1")
    revised = _scope(membership_revision="member-v2")
    store.activate(original)
    store.activate(revised)

    with pytest.raises(AuthorizationRejected, match="membership_revision_mismatch"):
        store.revoke_and_bump(original, reason="stale_session")
    assert store.read_state(revised).epoch == 1


def test_tenant_transition_revokes_previous_subject_scope(tmp_path):
    store = AuthorityStore(tmp_path / "control-plane")
    previous = _scope(tenant_id="tenant-a", session_id="session-a")
    current = _scope(tenant_id="tenant-b", session_id="session-b")

    assert store.activate(previous).epoch == 0
    state = store.activate(current)

    assert state.revoked_scope_digests == (previous.digest,)
    assert len(state.changes) == 1
    assert state.changes[0].scope_digest == previous.digest
    assert state.changes[0].revoked is True
    with pytest.raises(AuthorizationRejected, match="session_revoked"):
        store.read_state(previous)


def test_authority_changes_are_shared_and_ordered_across_store_clients(tmp_path):
    control_home = tmp_path / "control-plane"
    writer = AuthorityStore(control_home)
    reader = AuthorityStore(control_home)
    scope = _scope()

    writer.activate(scope)
    revoked = writer.revoke_and_bump(scope, reason="logout")

    changes = reader.changes_since(0)
    assert changes == revoked.changes
    assert changes[0].sequence > 0
    assert changes[0].scope_digest == scope.digest
    assert changes[0].epoch == revoked.epoch
    assert changes[0].revoked is True
    assert reader.changes_since(changes[0].sequence) == ()


def test_membership_change_invalidates_prior_epoch(tmp_path):
    store = AuthorityStore(tmp_path / "control-plane")
    original = _scope(membership_revision="member-v1")
    revised = _scope(membership_revision="member-v2")
    assert store.activate(original).epoch == 0

    current = store.activate(revised)
    assert current.epoch == 1
    assert current.revoked_scope_digests == (original.digest,)

    with pytest.raises(AuthorizationRejected, match="membership_revision_mismatch"):
        store.read_state(original)


def test_exact_audience_jti_can_only_be_consumed_once(tmp_path):
    store = AuthorityStore(tmp_path / "control-plane")
    scope = _scope()
    first = _consume(store, scope, jti="shared-jti")
    assert first.accepted is True

    with pytest.raises(AuthorizationRejected, match="credential_replayed"):
        _consume(store, scope, jti="shared-jti")


def test_concurrent_consumers_admit_exactly_one(tmp_path):
    control_home = tmp_path / "control-plane"
    scope = _scope()
    AuthorityStore(control_home).activate(scope)
    start = threading.Barrier(3)
    accepted: list[bool] = []
    rejected: list[str] = []
    result_lock = threading.Lock()

    def consume() -> None:
        store = AuthorityStore(control_home)
        start.wait()
        try:
            _consume(store, scope, jti="same-jti")
        except AuthorizationRejected as exc:
            with result_lock:
                rejected.append(exc.code)
        else:
            with result_lock:
                accepted.append(True)

    threads = [threading.Thread(target=consume) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert accepted == [True]
    assert rejected == ["credential_replayed"]


def test_wrong_audience_is_not_a_replay_key_collision(tmp_path):
    store = AuthorityStore(tmp_path / "control-plane")
    scope = _scope()
    state = store.activate(scope)
    assert store.check_and_consume(
        scope,
        token_class="browser-ws",
        issuer_key_version="bwt1",
        jti="same-jti",
        audience="browser-ws:/api/ws",
        expires_at=1_000,
        claim_epoch=state.epoch,
        claim_recovery_generation=state.recovery_generation,
        now=999,
    ).accepted
    assert store.check_and_consume(
        scope,
        token_class="browser-ws",
        issuer_key_version="bwt1",
        jti="same-jti",
        audience="browser-ws:/api/pty",
        expires_at=1_000,
        claim_epoch=state.epoch,
        claim_recovery_generation=state.recovery_generation,
        now=999,
    ).accepted


def test_expired_and_recovery_invalidated_credentials_are_rejected(tmp_path):
    store = AuthorityStore(tmp_path / "control-plane")
    scope = _scope()
    state = store.activate(scope)

    with pytest.raises(AuthorizationRejected, match="credential_expired"):
        store.check_and_consume(
            scope,
            token_class="browser-ws",
            issuer_key_version="bwt1",
            jti="expired",
            audience="browser-ws:/api/ws",
            expires_at=998,
            claim_epoch=state.epoch,
            claim_recovery_generation=state.recovery_generation,
            now=999,
        )

    store.invalidate_outstanding_credentials(reason="recovery_untrusted")
    with pytest.raises(AuthorizationRejected, match="recovery_generation_mismatch"):
        store.check_and_consume(
            scope,
            token_class="browser-ws",
            issuer_key_version="bwt1",
            jti="before-recovery",
            audience="browser-ws:/api/ws",
            expires_at=1_000,
            claim_epoch=state.epoch,
            claim_recovery_generation=state.recovery_generation,
            now=999,
        )


def test_worker_bootstrap_consumes_once_for_exact_active_lease(tmp_path):
    store = AuthorityStore(tmp_path / "control-plane")
    claim = store.claim_worker_start("ok1_a", worker_id="worker-a")
    active = store.transition_worker_lease(
        claim.lease, state=WorkerLeaseState.ACTIVE, generation_state=WorkerGenerationState.ACTIVE,
    )

    decision = store.check_and_consume_owner_worker_bootstrap(
        active,
        issuer_key_version="owc1-1",
        jti="bootstrap-a",
        audience="owner-worker-uds-bootstrap",
        expires_at=1_000,
        now=999,
    )
    assert decision.accepted is True
    assert decision.lease == active
    with pytest.raises(AuthorizationRejected, match="credential_replayed"):
        store.check_and_consume_owner_worker_bootstrap(
            active,
            issuer_key_version="owc1-1",
            jti="bootstrap-a",
            audience="owner-worker-uds-bootstrap",
            expires_at=1_000,
            now=999,
        )


def test_worker_bootstrap_rejects_non_active_or_stale_lease(tmp_path):
    store = AuthorityStore(tmp_path / "control-plane")
    claim = store.claim_worker_start("ok1_a", worker_id="worker-a")
    with pytest.raises(AuthorizationRejected, match="state_mismatch"):
        store.check_and_consume_owner_worker_bootstrap(
            claim.lease,
            issuer_key_version="owc1-1",
            jti="starting",
            audience="owner-worker-uds-bootstrap",
            expires_at=1_000,
            now=999,
        )
    active = store.transition_worker_lease(
        claim.lease, state=WorkerLeaseState.ACTIVE, generation_state=WorkerGenerationState.ACTIVE,
    )
    store.invalidate_outstanding_credentials(reason="recovery")
    with pytest.raises(AuthorizationRejected, match="stale"):
        store.check_and_consume_owner_worker_bootstrap(
            active,
            issuer_key_version="owc1-1",
            jti="stale",
            audience="owner-worker-uds-bootstrap",
            expires_at=1_000,
            now=999,
        )


def test_worker_lifecycle_changes_are_shared_and_exactly_fenced(tmp_path):
    control_home = tmp_path / "control-plane"
    writer = AuthorityStore(control_home)
    reader = AuthorityStore(control_home)
    claim = writer.claim_worker_start("ok1_a", worker_id="worker-a")
    active = writer.transition_worker_lease(
        claim.lease, state=WorkerLeaseState.ACTIVE, generation_state=WorkerGenerationState.ACTIVE,
    )

    draining = writer.transition_worker_lease(
        active, state=WorkerLeaseState.DRAINING, generation_state=WorkerGenerationState.DRAINING,
    )

    changes = reader.worker_changes_since(0)
    assert [change.lease_state for change in changes] == [WorkerLeaseState.ACTIVE, WorkerLeaseState.DRAINING]
    assert changes[-1].owner_key == draining.owner_key
    assert changes[-1].worker_generation == draining.worker_generation
    assert changes[-1].worker_id == draining.worker_id
    assert changes[-1].lease_version == draining.lease_version
    assert changes[-1].recovery_generation == draining.recovery_generation
    assert changes[-1].generation_state is WorkerGenerationState.DRAINING
    assert reader.worker_changes_since(changes[-1].sequence) == ()

    with pytest.raises(AuthorizationRejected, match="stale"):
        writer.transition_worker_lease(
            active, state=WorkerLeaseState.REVOKED, generation_state=WorkerGenerationState.REVOKED,
        )
    assert reader.worker_changes_since(changes[-1].sequence) == ()


def test_worker_lease_claim_is_fenced_and_rejects_stale_transition(tmp_path):
    store = AuthorityStore(tmp_path / "control-plane")
    claim = store.claim_worker_start("ok1_a", worker_id="worker-a")
    assert claim.lease.state is WorkerLeaseState.STARTING
    assert claim.lease.lease_version == 1

    active = store.transition_worker_lease(
        claim.lease, state=WorkerLeaseState.ACTIVE, generation_state=WorkerGenerationState.ACTIVE,
    )
    assert store.assert_worker_lease(active, states=frozenset({WorkerLeaseState.ACTIVE})) == active
    with pytest.raises(AuthorizationRejected, match="stale"):
        store.transition_worker_lease(
            claim.lease, state=WorkerLeaseState.REVOKED, generation_state=WorkerGenerationState.FAILED,
        )


def test_worker_lease_claim_rejects_concurrent_owner_and_allows_other_owner(tmp_path):
    store = AuthorityStore(tmp_path / "control-plane")
    a = store.claim_worker_start("ok1_a", worker_id="worker-a")
    with pytest.raises(AuthorizationRejected, match="already_owned"):
        AuthorityStore(tmp_path / "control-plane").claim_worker_start("ok1_a", worker_id="worker-a-2")
    b = store.claim_worker_start("ok1_b", worker_id="worker-b")
    assert a.lease.worker_generation == b.lease.worker_generation == 1


def test_authority_store_migrates_worker_generation_schema(tmp_path):
    control_home = tmp_path / "control-plane"
    control_home.mkdir()
    db_path = control_home / "authority.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE authority_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        conn.execute("INSERT INTO authority_meta(key, value) VALUES ('schema_version', 1)")
    db_path.chmod(0o600)

    record = AuthorityStore(control_home).allocate_worker_generation("ok1_a", worker_id="migrated")

    assert record.worker_generation == 1
    assert record.state is WorkerGenerationState.STARTING


def test_authority_store_migrates_v2_generation_history_to_v3_leases(tmp_path):
    control_home = tmp_path / "control-plane"
    control_home.mkdir()
    db_path = control_home / "authority.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE authority_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        conn.execute("INSERT INTO authority_meta(key, value) VALUES ('schema_version', 2)")
        conn.execute(
            "CREATE TABLE owner_worker_generations ("
            "owner_key TEXT NOT NULL, worker_generation INTEGER NOT NULL, worker_id TEXT NOT NULL UNIQUE, "
            "state TEXT NOT NULL, recovery_generation INTEGER NOT NULL, "
            "PRIMARY KEY(owner_key, worker_generation))"
        )
        conn.execute(
            "INSERT INTO owner_worker_generations VALUES ('ok1_a', 1, 'prior-worker', 'terminated', 0)"
        )
    db_path.chmod(0o600)

    claim = AuthorityStore(control_home).claim_worker_start("ok1_a", worker_id="replacement")

    assert claim.generation.worker_generation == 2
    assert claim.lease.lease_version == 1
    assert claim.lease.state is WorkerLeaseState.STARTING


def test_worker_lease_rejects_invalid_paired_lifecycle_transition(tmp_path):
    store = AuthorityStore(tmp_path / "control-plane")
    claim = store.claim_worker_start("ok1_a", worker_id="worker-a")

    with pytest.raises(AuthorizationRejected, match="worker_lease_invalid_transition"):
        store.transition_worker_lease(
            claim.lease,
            state=WorkerLeaseState.DRAINING,
            generation_state=WorkerGenerationState.DRAINING,
        )


def test_stale_worker_lease_cannot_finalize_replacement_after_recovery(tmp_path):
    store = AuthorityStore(tmp_path / "control-plane")
    first = store.claim_worker_start("ok1_a", worker_id="worker-a")
    active = store.transition_worker_lease(
        first.lease,
        state=WorkerLeaseState.ACTIVE,
        generation_state=WorkerGenerationState.ACTIVE,
    )
    store.invalidate_outstanding_credentials(reason="force replacement")
    replacement = store.claim_worker_start("ok1_a", worker_id="worker-b")

    with pytest.raises(AuthorizationRejected, match="stale"):
        store.transition_worker_lease(
            active,
            state=WorkerLeaseState.DRAINING,
            generation_state=WorkerGenerationState.DRAINING,
        )
    assert store.read_owner_worker_lease("ok1_a") == replacement.lease


def test_worker_generations_are_monotonic_per_owner_and_isolated(tmp_path):
    store = AuthorityStore(tmp_path / "control-plane")

    a_first = store.allocate_worker_generation("ok1_a", worker_id="worker-a-1")
    b_first = store.allocate_worker_generation("ok1_b", worker_id="worker-b-1")
    assert a_first.worker_generation == b_first.worker_generation == 1
    assert a_first.state is WorkerGenerationState.STARTING
    assert a_first.worker_id != b_first.worker_id

    active = store.transition_worker_generation(
        "ok1_a", 1, worker_id="worker-a-1", state=WorkerGenerationState.ACTIVE,
        expected_recovery_generation=a_first.recovery_generation,
    )
    draining = store.transition_worker_generation(
        "ok1_a", 1, worker_id="worker-a-1", state=WorkerGenerationState.DRAINING,
        expected_recovery_generation=active.recovery_generation,
    )
    store.transition_worker_generation(
        "ok1_a", 1, worker_id="worker-a-1", state=WorkerGenerationState.TERMINATED,
        expected_recovery_generation=draining.recovery_generation,
    )

    a_second = AuthorityStore(tmp_path / "control-plane").allocate_worker_generation("ok1_a", worker_id="worker-a-2")
    assert a_second.worker_generation == 2
    assert a_second.state is WorkerGenerationState.STARTING
    assert store.read_worker_generation("ok1_a", 1).state is WorkerGenerationState.TERMINATED


def test_worker_generation_rejects_invalid_transition_identity_and_recovery(tmp_path):
    store = AuthorityStore(tmp_path / "control-plane")
    record = store.allocate_worker_generation("ok1_a", worker_id="worker-a")

    with pytest.raises(AuthorizationRejected, match="identity_mismatch"):
        store.transition_worker_generation("ok1_a", 1, worker_id="worker-b", state=WorkerGenerationState.ACTIVE)
    with pytest.raises(AuthorizationRejected, match="invalid_transition"):
        store.transition_worker_generation("ok1_a", 1, worker_id="worker-a", state=WorkerGenerationState.TERMINATED)

    store.invalidate_outstanding_credentials(reason="test")
    with pytest.raises(AuthorizationRejected, match="recovery_mismatch"):
        store.transition_worker_generation(
            "ok1_a", 1, worker_id="worker-a", state=WorkerGenerationState.ACTIVE,
            expected_recovery_generation=record.recovery_generation,
        )
    with pytest.raises(AuthorizationRejected, match="not_found"):
        store.read_worker_generation("ok1_b", 1)


def test_unsafe_or_unavailable_storage_fails_closed(tmp_path):
    control_home = tmp_path / "control-plane"
    control_home.mkdir()
    db_path = control_home / "authority.sqlite3"
    db_path.symlink_to(tmp_path / "outside.sqlite3")

    with pytest.raises(AuthorityUnavailable, match="must not be a symlink"):
        AuthorityStore(control_home).activate(_scope())
