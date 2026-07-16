"""Focused tests for durable local dashboard account/session storage."""
from __future__ import annotations

import os
import sqlite3
import stat

import pytest

from hermes_cli.dashboard_auth.local_users import (
    LocalUserStore,
    LocalUserStoreConflict,
    LocalUserStoreUnavailable,
    normalize_username,
)


_SECRET = b"s" * 32


def _store(tmp_path, **kwargs) -> LocalUserStore:
    return LocalUserStore(secret=_SECRET, control_home=tmp_path / "control", **kwargs)


def _bootstrap(store: LocalUserStore):
    return store.bootstrap_accounts(
        [(f"user{i}", f"password-{i}-long-enough") for i in range(1, 6)],
        now=100,
    )


class TestAccountLifecycle:
    def test_bootstrap_is_exact_and_atomic(self, tmp_path):
        store = _store(tmp_path)
        with pytest.raises(ValueError):
            store.bootstrap_accounts(
                [("user1", "password-1-long-enough")], now=100
            )
        assert store.create_account(
            username="user1", password="password-1-long-enough", now=100
        ).username == "user1"
        assert store.get_account("user1") is not None

        second = _store(tmp_path / "exact")
        accounts = _bootstrap(second)
        assert len(accounts) == 5
        assert {account.username for account in accounts} == {
            "user1", "user2", "user3", "user4", "user5"
        }
        with pytest.raises(LocalUserStoreConflict):
            second.create_account(
                username="user6", password="password-6-long-enough", now=101
            )

    def test_bootstrap_rejects_duplicates_without_rows(self, tmp_path):
        store = _store(tmp_path)
        entries = [
            ("user1", "password-1-long-enough"),
            ("USER1", "password-2-long-enough"),
            ("user3", "password-3-long-enough"),
            ("user4", "password-4-long-enough"),
            ("user5", "password-5-long-enough"),
        ]
        with pytest.raises(ValueError):
            store.bootstrap_accounts(entries, now=100)
        assert store.create_account(
            username="user1", password="password-1-long-enough", now=100
        )

    def test_credentials_are_server_resolved_and_status_gated(self, tmp_path):
        store = _store(tmp_path)
        account = store.create_account(
            username="Alice", password="password-long-enough", now=100
        )
        assert account.username == "alice"
        assert store.verify_credentials(
            username="ALICE", password="password-long-enough"
        ) == account
        assert store.verify_credentials(username="missing", password="password-long-enough") is None
        assert store.verify_credentials(username="alice", password="wrong-password") is None

        disabled = store.set_account_status(username="alice", status="disabled", now=101)
        assert disabled.auth_revision == account.auth_revision + 1
        assert store.verify_credentials(username="alice", password="password-long-enough") is None

    def test_username_validation_is_canonical_and_bounded(self):
        assert normalize_username("  ALIce-1  ") == "alice-1"
        for invalid in ("ab", "a/b", "a b", "A!", "x" * 65):
            with pytest.raises(ValueError):
                normalize_username(invalid)

    def test_bootstrap_creates_members_without_implicit_administrator(self, tmp_path):
        accounts = _bootstrap(_store(tmp_path))
        assert all(account.role == "member" for account in accounts)
        assert all(account.status == "active" for account in accounts)
        assert all(not account.must_change_password for account in accounts)

    def test_last_active_admin_cannot_be_disabled_or_demoted(self, tmp_path):
        store = _store(tmp_path)
        admin = store.create_account(
            username="admin", password="password-long-enough", role="admin", now=100
        )
        with pytest.raises(LocalUserStoreConflict, match="last active admin"):
            store.set_account_status(username=admin.username, status="disabled", now=101)
        with pytest.raises(LocalUserStoreConflict, match="last active admin"):
            store.set_account_role(username=admin.username, role="member", now=101)

        store.create_account(
            username="alice", password="password-long-enough", role="admin", now=100
        )
        assert store.set_account_status(
            username=admin.username, status="disabled", now=101
        ).status == "disabled"

    def test_password_reset_records_must_change_password(self, tmp_path):
        store = _store(tmp_path)
        store.create_account(username="alice", password="password-long-enough", now=100)
        pending = store.set_password(
            username="alice", password="replacement-password", require_reset=True, now=101
        )
        assert pending.status == "active"
        assert pending.must_change_password is True
        assert store.verify_credentials(
            username="alice", password="replacement-password"
        ) == pending
        active = store.set_password(
            username="alice", password="another-password", now=102
        )
        assert active.status == "active"
        assert active.must_change_password is False

    def test_v1_accounts_migrate_to_members_transactionally(self, tmp_path):
        control = tmp_path / "control"
        control.mkdir(mode=0o700)
        path = control / "local-users.sqlite3"
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE local_user_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )
            conn.execute("INSERT INTO local_user_meta VALUES ('schema_version', 1)")
            conn.execute(
                "CREATE TABLE accounts ("
                "account_id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, "
                "display_name TEXT NOT NULL, password_hash TEXT NOT NULL, "
                "status TEXT NOT NULL, auth_revision INTEGER NOT NULL, "
                "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, "
                "password_changed_at INTEGER NOT NULL, disabled_at INTEGER)"
            )
            conn.execute(
                "INSERT INTO accounts VALUES (?, ?, ?, ?, 'active', 1, 100, 100, 100, NULL)",
                ("v1-account", "alice", "alice", "hash"),
            )
            conn.execute(
                "INSERT INTO accounts VALUES (?, ?, ?, ?, 'pending_reset', 1, 100, 100, 100, NULL)",
                ("v1-reset", "bob", "bob", "hash"),
            )
        path.chmod(0o600)
        migrated = _store(tmp_path).get_account("alice")
        reset_required = _store(tmp_path).get_account("bob")
        assert migrated is not None
        assert migrated.role == "member"
        assert migrated.status == "active"
        assert migrated.must_change_password is False
        assert reset_required is not None
        assert reset_required.role == "member"
        assert reset_required.status == "active"
        assert reset_required.must_change_password is True


class TestSessionLifecycle:
    def test_session_survives_new_store_instance_with_same_secret(self, tmp_path):
        store = _store(tmp_path)
        account = store.create_account(
            username="alice", password="password-long-enough", now=100
        )
        session = store.create_session(
            account=account, access_ttl_seconds=60, refresh_ttl_seconds=600, now=100
        )
        restarted = _store(tmp_path)
        verified = restarted.verify_access_token(session.access_token, now=101)
        assert verified is not None
        assert verified.account.account_id == account.account_id
        assert verified.session_id == session.session_id

        wrong_secret = LocalUserStore(
            secret=b"x" * 32, control_home=tmp_path / "control"
        )
        assert wrong_secret.verify_access_token(session.access_token, now=101) is None

    def test_rotate_once_and_detect_refresh_reuse(self, tmp_path):
        store = _store(tmp_path)
        account = store.create_account(
            username="alice", password="password-long-enough", now=100
        )
        session = store.create_session(
            account=account, access_ttl_seconds=60, refresh_ttl_seconds=600, now=100
        )
        rotated = store.rotate_refresh_token(
            session.refresh_token,
            access_ttl_seconds=60,
            refresh_ttl_seconds=600,
            now=110,
        )
        assert rotated is not None
        assert rotated.access_token != session.access_token
        assert rotated.refresh_token != session.refresh_token
        assert store.verify_access_token(session.access_token, now=110) is None
        assert store.verify_access_token(rotated.access_token, now=110) is not None

        # Reusing the replaced refresh credential invalidates every account
        # session, including the freshly rotated access token.
        assert store.rotate_refresh_token(
            session.refresh_token,
            access_ttl_seconds=60,
            refresh_ttl_seconds=600,
            now=111,
        ) is None
        assert store.verify_access_token(rotated.access_token, now=111) is None

    def test_password_change_and_revoke_all_invalidate_access(self, tmp_path):
        store = _store(tmp_path)
        account = store.create_account(
            username="alice", password="password-long-enough", now=100
        )
        first = store.create_session(
            account=account, access_ttl_seconds=60, refresh_ttl_seconds=600, now=100
        )
        changed = store.set_password(
            username="alice", password="replacement-password-long", now=101
        )
        assert changed.auth_revision == account.auth_revision + 1
        assert store.verify_access_token(first.access_token, now=101) is None
        assert store.verify_credentials(
            username="alice", password="password-long-enough"
        ) is None
        assert store.verify_credentials(
            username="alice", password="replacement-password-long"
        ) == changed

        second = store.create_session(
            account=changed, access_ttl_seconds=60, refresh_ttl_seconds=600, now=102
        )
        store.revoke_all_sessions(username="alice", now=103)
        assert store.verify_access_token(second.access_token, now=103) is None

    def test_token_prefixes_prevent_foreign_token_lookup(self, tmp_path):
        store = _store(tmp_path)
        assert store.verify_access_token("some-other-provider-token", now=100) is None
        assert store.rotate_refresh_token(
            "hlu1.at." + "a" * 43,
            access_ttl_seconds=60,
            refresh_ttl_seconds=600,
            now=100,
        ) is None


class TestStoreSafety:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission contract")
    def test_rejects_unsafe_control_home_and_database_modes(self, tmp_path):
        control = tmp_path / "control"
        control.mkdir(mode=0o777)
        control.chmod(0o777)
        unsafe = LocalUserStore(secret=_SECRET, control_home=control)
        with pytest.raises(LocalUserStoreUnavailable):
            unsafe.create_account(
                username="alice", password="password-long-enough", now=100
            )

        safe = _store(tmp_path / "safe")
        safe.create_account(
            username="alice", password="password-long-enough", now=100
        )
        assert stat.S_IMODE(safe.path.stat().st_mode) == 0o600
        safe.path.chmod(0o644)
        with pytest.raises(LocalUserStoreUnavailable):
            safe.get_account("alice")

    @pytest.mark.skipif(os.name == "nt", reason="POSIX symlink contract")
    def test_rejects_symlink_database(self, tmp_path):
        control = tmp_path / "control"
        control.mkdir(mode=0o700)
        target = tmp_path / "target.sqlite3"
        target.write_text("not a database")
        (control / "local-users.sqlite3").symlink_to(target)
        store = LocalUserStore(secret=_SECRET, control_home=control)
        with pytest.raises(LocalUserStoreUnavailable):
            store.create_account(
                username="alice", password="password-long-enough", now=100
            )

    def test_missing_or_weak_master_secret_is_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            LocalUserStore(secret=b"too-short", control_home=tmp_path)
