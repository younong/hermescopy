"""Focused tests for first-class employee domain storage."""

from __future__ import annotations

import base64
import sqlite3

import pytest

from hermes_cli.channel_identity import (
    ChannelCrypto,
    ChannelIdentityStore,
    EmployeeProfileRevisionConflict,
    FeishuCredentialRevisionConflict,
    Keyring,
    create_employee,
    list_employees,
    register_employee_feishu_binding,
    resolve_employee,
    resolve_employee_feishu_credentials,
    resolve_employee_profile,
    rotate_employee_feishu_credentials,
    set_employee_feishu_binding_status,
    set_employee_status,
    update_employee_collaboration_policy,
    update_employee_profile,
)
from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.owner_context import owner_context_from_session


def _owner(user_id: str):
    return owner_context_from_session(
        Session(
            user_id=user_id,
            email=f"{user_id}@example.com",
            display_name=user_id,
            org_id="org-a",
            provider="stub",
            expires_at=9_999_999_999,
            access_token="access",
            refresh_token="refresh",
        )
    )


@pytest.fixture
def crypto() -> ChannelCrypto:
    return ChannelCrypto(
        lookup=Keyring(keys={1: b"l" * 32}, active_version=1),
        encryption=Keyring(keys={1: b"e" * 32}, active_version=1),
    )


@pytest.fixture
def store(tmp_path, crypto, monkeypatch) -> ChannelIdentityStore:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_OWNER_SECRET", "owner-secret")
    return ChannelIdentityStore(crypto, tmp_path / "control", global_home=tmp_path)


def test_fresh_schema_only_contains_generic_employee_tables(store):
    with store.read() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        version = conn.execute(
            "SELECT value FROM channel_identity_meta WHERE key='schema_version'"
        ).fetchone()["value"]
    assert version == "15"
    assert {
        "employees",
        "employee_profiles",
        "employee_collaboration_policies",
        "employee_channel_bindings",
    } <= tables
    assert not {
        "managed_feishu_accounts",
        "feishu_employee_profiles",
        "feishu_employee_collaboration_policies",
    } & tables


def test_v14_upgrade_replaces_empty_legacy_employee_tables(tmp_path, crypto, monkeypatch):
    monkeypatch.setenv("HERMES_OWNER_SECRET", "owner-secret")
    first = ChannelIdentityStore(crypto, tmp_path / "control", global_home=tmp_path)
    with first.write() as conn:
        for table in (
            "employee_channel_bindings",
            "employee_collaboration_policies",
            "employee_profiles",
            "employees",
        ):
            conn.execute(f"DROP TABLE {table}")
        conn.execute(
            "CREATE TABLE managed_feishu_accounts (account_id TEXT PRIMARY KEY)"
        )
        conn.execute(
            "CREATE TABLE feishu_employee_profiles (account_id TEXT, revision INTEGER)"
        )
        conn.execute(
            "CREATE TABLE feishu_employee_collaboration_policies (account_id TEXT PRIMARY KEY)"
        )
        conn.execute(
            "UPDATE channel_identity_meta SET value='14' WHERE key='schema_version'"
        )

    migrated = ChannelIdentityStore(crypto, tmp_path / "control", global_home=tmp_path)
    with migrated.read() as conn:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert "employees" in tables
    assert "managed_feishu_accounts" not in tables


def test_v14_upgrade_refuses_nonempty_legacy_employee_tables(
    tmp_path, crypto, monkeypatch
):
    monkeypatch.setenv("HERMES_OWNER_SECRET", "owner-secret")
    first = ChannelIdentityStore(crypto, tmp_path / "control", global_home=tmp_path)
    with first.write() as conn:
        for table in (
            "employee_channel_bindings",
            "employee_collaboration_policies",
            "employee_profiles",
            "employees",
        ):
            conn.execute(f"DROP TABLE {table}")
        conn.execute(
            "CREATE TABLE managed_feishu_accounts (account_id TEXT PRIMARY KEY)"
        )
        conn.execute(
            "CREATE TABLE feishu_employee_profiles (account_id TEXT, revision INTEGER)"
        )
        conn.execute(
            "CREATE TABLE feishu_employee_collaboration_policies (account_id TEXT PRIMARY KEY)"
        )
        conn.execute("INSERT INTO managed_feishu_accounts VALUES ('legacy')")
        conn.execute(
            "UPDATE channel_identity_meta SET value='14' WHERE key='schema_version'"
        )

    with pytest.raises(RuntimeError, match="legacy Feishu employee data"):
        ChannelIdentityStore(crypto, tmp_path / "control", global_home=tmp_path)
    with sqlite3.connect(first.path) as conn:
        version = conn.execute(
            "SELECT value FROM channel_identity_meta WHERE key='schema_version'"
        ).fetchone()[0]
    assert version == "14"


def test_employee_crud_profiles_policy_and_owner_security(store):
    owner = _owner("owner-a")
    other = _owner("owner-b")
    employee = create_employee(
        store, owner=owner, profile={"name": "Analyst", "tools": ["web"]}
    )

    assert employee.employee_id.startswith("emp_")
    assert employee.feishu_binding is None
    assert employee.collaboration_policy.employee_id == employee.employee_id
    assert [item.employee_id for item in list_employees(store, owner=owner)] == [
        employee.employee_id
    ]
    assert list_employees(store, owner=other) == ()
    with pytest.raises(RuntimeError, match="unavailable"):
        resolve_employee(store, owner=other, employee_id=employee.employee_id)

    profile = resolve_employee_profile(
        store, owner=owner, employee_id=employee.employee_id
    )
    assert profile.employee_id == employee.employee_id
    assert profile.profile["name"] == "Analyst"
    updated = update_employee_profile(
        store,
        owner=owner,
        employee_id=employee.employee_id,
        profile={"name": "Senior Analyst"},
        expected_revision=1,
    )
    assert updated.revision == 2
    with pytest.raises(EmployeeProfileRevisionConflict):
        update_employee_profile(
            store,
            owner=owner,
            employee_id=employee.employee_id,
            profile={"name": "stale"},
            expected_revision=1,
        )
    policy = update_employee_collaboration_policy(
        store,
        owner=owner,
        employee_id=employee.employee_id,
        may_participate=False,
        may_create_groups=True,
        invite_quota=None,
    )
    assert policy.employee_id == employee.employee_id
    assert policy.invite_quota is None

    suspended = set_employee_status(
        store, owner=owner, employee_id=employee.employee_id, status="suspended"
    )
    assert suspended.lifecycle_status == "suspended"
    with store.write() as conn:
        other_canonical_user_id = conn.execute(
            "SELECT canonical_user_id FROM owner_bindings WHERE owner_key=?",
            (other.owner_key,),
        ).fetchone()
        assert other_canonical_user_id is None
    with pytest.raises(sqlite3.IntegrityError, match="Owner is immutable"):
        with store.write() as conn:
            conn.execute(
                "UPDATE employees SET canonical_user_id='attacker' WHERE employee_id=?",
                (employee.employee_id,),
            )


def test_feishu_binding_lifecycle_credentials_and_rebind_are_separate(store):
    owner = _owner("owner-a")
    employee = create_employee(store, owner=owner, profile={"name": "Analyst"})
    first = register_employee_feishu_binding(
        store,
        owner=owner,
        employee_id=employee.employee_id,
        provider_account_id="app-a",
        credentials={"app_id": "app-a", "app_secret": "old"},
    )
    assert first.binding_id.startswith("ecb_")
    assert first.connector_account_id.startswith("ca_")
    with pytest.raises(RuntimeError, match="already has"):
        register_employee_feishu_binding(
            store,
            owner=owner,
            employee_id=employee.employee_id,
            provider_account_id="app-b",
            credentials={"app_id": "app-b", "app_secret": "secret"},
        )

    rotated = rotate_employee_feishu_credentials(
        store,
        owner=owner,
        employee_id=employee.employee_id,
        credentials={"app_id": "app-a", "app_secret": "new"},
        expected_credential_version=1,
    )
    assert rotated.credential_version == 2
    credentials, version = resolve_employee_feishu_credentials(
        store, owner=owner, employee_id=employee.employee_id
    )
    assert credentials["app_secret"] == "new"
    assert version == 2
    with pytest.raises(FeishuCredentialRevisionConflict):
        rotate_employee_feishu_credentials(
            store,
            owner=owner,
            employee_id=employee.employee_id,
            credentials={"app_id": "app-a", "app_secret": "stale"},
            expected_credential_version=1,
        )

    set_employee_status(
        store, owner=owner, employee_id=employee.employee_id, status="suspended"
    )
    assert resolve_employee_feishu_credentials(
        store, owner=owner, employee_id=employee.employee_id
    )[0]["app_secret"] == "new"
    revoked = set_employee_feishu_binding_status(
        store,
        owner=owner,
        employee_id=employee.employee_id,
        status="revoked",
    )
    assert revoked.lifecycle_status == "revoked"
    replacement = register_employee_feishu_binding(
        store,
        owner=owner,
        employee_id=employee.employee_id,
        provider_account_id="app-b",
        credentials={"app_id": "app-b", "app_secret": "replacement"},
    )
    assert replacement.binding_id != first.binding_id
    assert resolve_employee(
        store, owner=owner, employee_id=employee.employee_id
    ).lifecycle_status == "suspended"


def test_employee_profile_key_version_is_validated(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_OWNER_SECRET", "owner-secret")
    crypto = ChannelCrypto(
        lookup=Keyring(keys={1: b"l" * 32}, active_version=1),
        encryption=Keyring(keys={1: b"e" * 32, 2: b"f" * 32}, active_version=2),
    )
    store = ChannelIdentityStore(crypto, tmp_path / "control", global_home=tmp_path)
    create_employee(store, owner=_owner("owner-a"), profile={"name": "Analyst"})

    missing_key_crypto = ChannelCrypto(
        lookup=Keyring(keys={1: b"l" * 32}, active_version=1),
        encryption=Keyring(keys={1: b"e" * 32}, active_version=1),
    )
    with pytest.raises(RuntimeError, match="required key version 2 is unavailable"):
        ChannelIdentityStore(
            missing_key_crypto, tmp_path / "control", global_home=tmp_path
        )
