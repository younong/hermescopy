"""Tests for the WS-upgrade ticket store (Phase 5 task 5.1).

The store is process-local and threading-safe. Tests run with xdist so
each worker has its own module instance — no cross-worker bleed — but we
call ``_reset_for_tests`` between tests to keep things deterministic.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import FrozenInstanceError

import pytest

from hermes_cli.dashboard_auth import ws_tickets
from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.owner_context import (
    begin_owner_key_rotation,
    complete_owner_key_rotation,
    ensure_owner_home,
    migrate_owner_home_for_rotation,
    owner_context_from_owner_key,
    owner_context_from_session,
    owner_context_from_ticket_payload,
    owner_keyring_backup_paths,
    owner_worker_env,
    tenant_id_from_session,
)
from hermes_cli.dashboard_auth.ws_tickets import (
    TTL_SECONDS,
    TicketInvalid,
    _reset_for_tests,
    consume_ticket,
    mint_ticket,
)


@pytest.fixture(autouse=True)
def _reset():
    _reset_for_tests()
    yield
    _reset_for_tests()


def _session(*, user_id: str = "u1", org_id: str = "org-1", provider: str = "stub") -> Session:
    return Session(
        user_id=user_id,
        email=f"{user_id}@example.test",
        display_name="Test User",
        org_id=org_id,
        provider=provider,
        expires_at=1234567890,
        access_token="at",
        refresh_token="rt",
    )


# ---------------------------------------------------------------------------
# Owner context derivation
# ---------------------------------------------------------------------------


class TestOwnerContext:
    def test_same_verified_session_derives_one_stable_immutable_context(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_OWNER_SECRET", "test-owner-secret")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "global"))

        first = owner_context_from_session(_session())
        second = owner_context_from_session(_session())

        assert first == second
        assert first.auth_provider == "stub"
        assert first.tenant_id == "org-1"
        assert first.owner_user_id == "u1"
        assert re.fullmatch(r"ok1_[A-Za-z0-9_.-]+", first.owner_key)
        assert first.host_global_home == tmp_path / "global"
        assert first.host_owner_home == tmp_path / "global" / "users" / first.owner_key
        assert first.owner_home == first.host_owner_home
        with pytest.raises(FrozenInstanceError):
            first.owner_key = "ok1_other"  # type: ignore[misc]

    def test_distinct_trusted_principal_material_is_owner_isolated(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_OWNER_SECRET", "test-owner-secret")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "global"))

        owner_a = owner_context_from_session(_session(user_id="user-a", org_id="org-a", provider="stub"))
        different_user = owner_context_from_session(_session(user_id="user-b", org_id="org-a", provider="stub"))
        different_tenant = owner_context_from_session(_session(user_id="user-a", org_id="org-b", provider="stub"))
        different_provider = owner_context_from_session(_session(user_id="user-a", org_id="org-a", provider="other"))

        assert len({owner_a.owner_key, different_user.owner_key, different_tenant.owner_key, different_provider.owner_key}) == 4
        assert len({owner_a.owner_home, different_user.owner_home, different_tenant.owner_home, different_provider.owner_home}) == 4

    def test_session_without_trusted_user_id_fails_closed(self, monkeypatch):
        monkeypatch.setenv("HERMES_OWNER_SECRET", "test-owner-secret")

        with pytest.raises(ValueError, match="session.user_id is required"):
            owner_context_from_session(_session(user_id="   "))

    def test_personal_tenant_is_provider_scoped_when_org_id_empty(self, monkeypatch):
        monkeypatch.setenv("HERMES_OWNER_SECRET", "test-owner-secret")
        first = owner_context_from_session(_session(org_id="", provider="stub"))
        second = owner_context_from_session(_session(org_id="", provider="stub"))
        other_provider = owner_context_from_session(_session(org_id="", provider="other"))

        assert first.tenant_id == "personal:stub"
        assert tenant_id_from_session(_session(org_id="", provider="stub")) == "personal:stub"
        assert first.owner_key == second.owner_key
        assert other_provider.tenant_id == "personal:other"
        # ok1 keeps legacy personal tenant material internally, but provider is
        # still a separate HMAC input, so same-user personal identities from
        # different providers remain owner-isolated.
        assert other_provider.owner_key != first.owner_key

    def test_owner_secret_persists_under_global_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HERMES_OWNER_SECRET", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        first = owner_context_from_session(_session())
        secret_path = tmp_path / "control-plane" / "owner_secret"
        assert secret_path.exists()
        assert (first.owner_home.parent, first.owner_home.name) == (
            tmp_path / "users",
            first.owner_key,
        )

        second = owner_context_from_session(_session())
        assert second.owner_key == first.owner_key

    def test_env_owner_secret_must_match_persisted_secret(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        secret_path = tmp_path / "control-plane" / "owner_secret"
        secret_path.parent.mkdir(parents=True)
        secret_path.write_text("persisted-secret\n", encoding="utf-8")
        monkeypatch.setenv("HERMES_OWNER_SECRET", "different-secret")

        with pytest.raises(RuntimeError, match="does not match persisted owner secret"):
            owner_context_from_session(_session())

    def test_owner_key_reconstruction_requires_global_home_in_worker(self, monkeypatch, tmp_path):
        owner_key = "ok1_abcdef123456"
        monkeypatch.setenv("HERMES_OWNER_KEY", owner_key)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "global" / "users" / owner_key))

        with pytest.raises(RuntimeError, match="explicit global_home"):
            owner_context_from_owner_key(owner_key)

        owner = owner_context_from_owner_key(owner_key, global_home=tmp_path / "global")
        assert owner.owner_home == (tmp_path / "global" / "users" / owner_key).resolve()

    def test_ticket_payload_reconstructs_same_owner_as_http_session(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_OWNER_SECRET", "test-owner-secret")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "global"))
        owner = owner_context_from_session(_session())
        ticket = mint_ticket(
            user_id="u1",
            provider="stub",
            org_id="org-1",
            tenant_id=owner.tenant_id,
            owner_key=owner.owner_key,
        )

        payload = consume_ticket(ticket)
        from_payload = owner_context_from_ticket_payload(payload)

        assert from_payload == owner
        assert owner_worker_env(from_payload)["HERMES_OWNER_KEY"] == owner.owner_key
        assert owner_worker_env(from_payload)["HERMES_HOME"] == str(owner.owner_home)

    def test_ticket_for_owner_a_cannot_reconstruct_owner_b(self, monkeypatch):
        monkeypatch.setenv("HERMES_OWNER_SECRET", "test-owner-secret")
        owner_a = owner_context_from_session(_session(user_id="user-a"))
        owner_b = owner_context_from_session(_session(user_id="user-b"))
        ticket = mint_ticket(
            user_id="user-a",
            provider="stub",
            org_id="org-1",
            tenant_id=owner_a.tenant_id,
            owner_key=owner_b.owner_key,
        )

        with pytest.raises(ValueError, match="owner_key mismatch"):
            owner_context_from_ticket_payload(consume_ticket(ticket))

    def test_ticket_payload_rejects_tenant_id_mismatch(self, monkeypatch):
        monkeypatch.setenv("HERMES_OWNER_SECRET", "test-owner-secret")
        owner = owner_context_from_session(_session())
        with pytest.raises(ValueError, match="tenant_id mismatch"):
            owner_context_from_ticket_payload({
                "user_id": "u1",
                "provider": "stub",
                "org_id": "org-1",
                "tenant_id": "other-tenant",
                "owner_key": owner.owner_key,
            })

    def test_ticket_payload_rejects_owner_key_mismatch(self, monkeypatch):
        monkeypatch.setenv("HERMES_OWNER_SECRET", "test-owner-secret")
        owner = owner_context_from_session(_session())
        with pytest.raises(ValueError, match="owner_key mismatch"):
            owner_context_from_ticket_payload({
                "user_id": "u1",
                "provider": "stub",
                "org_id": "org-1",
                "tenant_id": owner.tenant_id,
                "owner_key": "ok1_wrong",
            })

    def test_keyring_backup_includes_active_and_retained_secret_versions(self, monkeypatch, tmp_path):
        monkeypatch.delenv("HERMES_OWNER_SECRET", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        before = owner_context_from_session(_session(user_id="user-a"))

        begin_owner_key_rotation("rotation-secret")
        during = owner_context_from_session(_session(user_id="user-a"))
        backup_paths = owner_keyring_backup_paths()

        assert during.owner_key == before.owner_key
        assert backup_paths == (tmp_path / "control-plane" / "owner_keyring.json",)

    def test_explicit_rotation_migrates_owner_home_then_switches_active_key(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_OWNER_SECRET", "old-secret")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        before = owner_context_from_session(_session(user_id="user-a"))
        ensure_owner_home(before)
        (before.owner_home / "sessions" / "state.txt").write_text("owner state", encoding="utf-8")

        begin_owner_key_rotation("new-secret")
        assert owner_context_from_session(_session(user_id="user-a")) == before
        migrate_owner_home_for_rotation(before)
        complete_owner_key_rotation()
        monkeypatch.setenv("HERMES_OWNER_SECRET", "new-secret")
        after = owner_context_from_session(_session(user_id="user-a"))

        assert after.owner_key != before.owner_key
        assert not before.owner_home.exists()
        assert (after.owner_home / "sessions" / "state.txt").read_text(encoding="utf-8") == "owner state"

    def test_rotation_rejects_destination_conflict_and_preserves_active_key(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_OWNER_SECRET", "old-secret")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        before = owner_context_from_session(_session(user_id="user-a"))
        ensure_owner_home(before)

        begin_owner_key_rotation("new-secret")
        target = tmp_path / "users" / "ok1_unrelated"
        target.mkdir(parents=True)
        with pytest.raises(RuntimeError, match="destination owner home already exists"):
            migrate_owner_home_for_rotation(before, destination_owner_key="ok1_unrelated")
        with pytest.raises(RuntimeError, match="owner home migration is incomplete"):
            complete_owner_key_rotation()
        assert owner_context_from_session(_session(user_id="user-a")) == before

    def test_rotation_requires_explicit_pending_state(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_OWNER_SECRET", "old-secret")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        with pytest.raises(RuntimeError, match="no owner key rotation is pending"):
            complete_owner_key_rotation()
        with pytest.raises(ValueError, match="non-empty"):
            begin_owner_key_rotation("  ")

    def test_ensure_owner_home_rejects_symlinked_users_root(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_OWNER_SECRET", "test-owner-secret")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "global"))
        owner = owner_context_from_session(_session())
        outside = tmp_path / "outside"
        outside.mkdir()
        owner.host_global_home.mkdir(exist_ok=True)
        try:
            (owner.host_global_home / "users").symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable")

        with pytest.raises(RuntimeError, match="must not be a symlink"):
            ensure_owner_home(owner)

    def test_ensure_owner_home_rejects_unsafe_existing_owner_permissions(self, monkeypatch, tmp_path):
        if os.name == "nt":
            pytest.skip("POSIX mode bits unavailable")
        monkeypatch.setenv("HERMES_OWNER_SECRET", "test-owner-secret")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "global"))
        owner = owner_context_from_session(_session())
        owner.host_owner_home.mkdir(parents=True)
        owner.host_owner_home.chmod(0o755)

        with pytest.raises(RuntimeError, match="unsafe permissions"):
            ensure_owner_home(owner)

    def test_ensure_owner_home_and_worker_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_OWNER_SECRET", "test-owner-secret")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        owner = owner_context_from_session(_session())

        ensure_owner_home(owner)

        for rel in (
            "runtime/logs",
            "runtime/checkpoints",
            "sessions",
            "workspaces/default",
            "skills",
            "memories",
        ):
            assert (owner.owner_home / rel).is_dir()
        env = owner_worker_env(owner)
        assert env["HERMES_HOME"] == str(owner.owner_home)
        assert env["HERMES_OWNER_KEY"] == owner.owner_key
        assert env["HERMES_TENANT_ID"] == owner.tenant_id
        assert env["HERMES_OWNER_USER_ID"] == owner.owner_user_id
        assert env["HERMES_AUTH_PROVIDER"] == owner.auth_provider
        assert env["HERMES_WORKSPACE_ROOT"] == str(owner.owner_home / "workspaces")


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestMintAndConsume:
    def test_round_trip(self):
        ticket = mint_ticket(
            user_id="u1",
            provider="nous",
            org_id="org-1",
            tenant_id="org-1",
            owner_key="ok1_owner",
        )
        info = consume_ticket(ticket)
        assert info["user_id"] == "u1"
        assert info["provider"] == "nous"
        assert info["org_id"] == "org-1"
        assert info["tenant_id"] == "org-1"
        assert info["owner_key"] == "ok1_owner"
        assert "minted_at" in info
        assert info["expires_at"] == info["minted_at"] + TTL_SECONDS

    def test_ticket_has_minimum_length(self):
        # ``secrets.token_urlsafe(32)`` produces ~43 chars; enforce a floor
        # so a future refactor can't accidentally shrink the entropy.
        ticket = mint_ticket(user_id="u1", provider="nous")
        assert len(ticket) >= 32

    def test_ticket_values_are_unique(self):
        seen = {mint_ticket(user_id="u1", provider="x") for _ in range(50)}
        assert len(seen) == 50


# ---------------------------------------------------------------------------
# Single-use
# ---------------------------------------------------------------------------


class TestSingleUse:
    def test_second_consume_raises(self):
        ticket = mint_ticket(user_id="u1", provider="stub")
        consume_ticket(ticket)
        with pytest.raises(TicketInvalid, match="unknown"):
            consume_ticket(ticket)

    def test_unknown_ticket_rejected(self):
        with pytest.raises(TicketInvalid, match="unknown"):
            consume_ticket("nope-never-minted")

    def test_empty_ticket_rejected(self):
        with pytest.raises(TicketInvalid):
            consume_ticket("")


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------


class TestTTL:
    def test_constant_is_30_seconds(self):
        # Pinned so a refactor that doubled the lifetime would surface here.
        assert TTL_SECONDS == 30

    def test_expired_ticket_rejected(self, monkeypatch):
        # Mock time inside the ws_tickets module so mint and consume see
        # different clocks. We have to patch the symbol the module actually
        # binds; ``time`` is module-level there.
        clock = {"now": 1_000_000}

        def fake_time():
            return clock["now"]

        monkeypatch.setattr(ws_tickets.time, "time", fake_time)

        ticket = mint_ticket(user_id="u1", provider="stub")
        clock["now"] += TTL_SECONDS + 1
        with pytest.raises(TicketInvalid, match="expired"):
            consume_ticket(ticket)

    def test_at_exact_ttl_boundary_still_valid(self, monkeypatch):
        clock = {"now": 1_000_000}
        monkeypatch.setattr(ws_tickets.time, "time", lambda: clock["now"])

        ticket = mint_ticket(user_id="u1", provider="stub")
        clock["now"] += TTL_SECONDS  # exactly at boundary; expires_at == now
        # Implementation: ``expires_at < now`` (strict), so == passes.
        info = consume_ticket(ticket)
        assert info["user_id"] == "u1"


# ---------------------------------------------------------------------------
# Truncated value in error message (secret hygiene)
# ---------------------------------------------------------------------------


class TestErrorMessages:
    def test_unknown_ticket_error_truncates_value(self):
        long_value = "a" * 100
        with pytest.raises(TicketInvalid) as exc_info:
            consume_ticket(long_value)
        # Never log more than the first 8 chars of an opaque ticket.
        message = str(exc_info.value)
        assert long_value not in message
        assert long_value[:8] in message


# ---------------------------------------------------------------------------
# Thread safety: mint + consume from many threads doesn't deadlock or
# return duplicates.
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_mint_and_consume_concurrent(self):
        results: list[dict] = []
        errors: list[Exception] = []
        lock = threading.Lock()

        def worker(i: int):
            try:
                t = mint_ticket(user_id=f"u{i}", provider="stub")
                info = consume_ticket(t)
                with lock:
                    results.append(info)
            except Exception as exc:  # noqa: BLE001 — collect for assert
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
            assert not t.is_alive(), "thread deadlocked"

        assert errors == []
        assert len(results) == 20
        # Every consume returns a distinct user_id (no cross-thread bleed).
        assert {r["user_id"] for r in results} == {f"u{i}" for i in range(20)}


# ---------------------------------------------------------------------------
# Process-lifetime internal credential (server-spawned PTY child auth).
# Direct unit coverage for internal_ws_credential / consume_internal_credential
# — _ws_auth_ok exercises these indirectly, but the mint-once, unminted, and
# empty-value branches are only reachable via direct calls.
# ---------------------------------------------------------------------------


class TestInternalCredential:
    def test_minted_once_is_stable(self):
        """Successive calls return the same process-lifetime value."""
        first = ws_tickets.internal_ws_credential()
        second = ws_tickets.internal_ws_credential()
        assert first == second
        assert len(first) >= 32  # token_urlsafe(32)

    def test_round_trip_identity(self):
        cred = ws_tickets.internal_ws_credential()
        info = ws_tickets.consume_internal_credential(cred)
        assert info["user_id"] == ws_tickets.INTERNAL_USER_ID
        assert info["provider"] == ws_tickets.INTERNAL_PROVIDER

    def test_multi_use(self):
        """Unlike a single-use ticket, the credential survives repeated consume."""
        cred = ws_tickets.internal_ws_credential()
        for _ in range(5):
            assert (
                ws_tickets.consume_internal_credential(cred)["provider"]
                == ws_tickets.INTERNAL_PROVIDER
            )

    def test_rejected_before_mint(self):
        """With nothing minted yet, any value is rejected (expected is None)."""
        # autouse _reset leaves _internal_credential == None at test start.
        with pytest.raises(TicketInvalid):
            ws_tickets.consume_internal_credential("anything")

    def test_empty_value_rejected(self):
        ws_tickets.internal_ws_credential()  # mint so expected is non-None
        with pytest.raises(TicketInvalid):
            ws_tickets.consume_internal_credential("")

    def test_wrong_value_rejected(self):
        ws_tickets.internal_ws_credential()
        with pytest.raises(TicketInvalid):
            ws_tickets.consume_internal_credential("not-the-credential")

    def test_reset_clears_and_remints(self):
        first = ws_tickets.internal_ws_credential()
        _reset_for_tests()
        # The old value no longer validates after reset.
        with pytest.raises(TicketInvalid):
            ws_tickets.consume_internal_credential(first)
        # A fresh mint produces a different value.
        second = ws_tickets.internal_ws_credential()
        assert second != first
        assert ws_tickets.consume_internal_credential(second)["user_id"] == (
            ws_tickets.INTERNAL_USER_ID
        )

    def test_independent_of_ticket_store(self):
        """The internal credential is not a ticket — minting tickets doesn't
        touch it, and consuming the credential doesn't consume tickets."""
        cred = ws_tickets.internal_ws_credential()
        ticket = mint_ticket(user_id="u1", provider="nous")
        # Consuming the internal credential leaves the ticket intact.
        ws_tickets.consume_internal_credential(cred)
        assert consume_ticket(ticket)["user_id"] == "u1"
