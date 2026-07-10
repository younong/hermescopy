"""Unit tests for the Control Plane authority/replay store."""
from __future__ import annotations

import threading

import pytest

from hermes_cli.dashboard_auth.authority import (
    AuthorityStore,
    AuthorityUnavailable,
    AuthorizationRejected,
    AuthorizationScope,
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


def test_unsafe_or_unavailable_storage_fails_closed(tmp_path):
    control_home = tmp_path / "control-plane"
    control_home.mkdir()
    db_path = control_home / "authority.sqlite3"
    db_path.symlink_to(tmp_path / "outside.sqlite3")

    with pytest.raises(AuthorityUnavailable, match="must not be a symlink"):
        AuthorityStore(control_home).activate(_scope())
