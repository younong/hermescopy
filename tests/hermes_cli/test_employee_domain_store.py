"""Focused tests for first-class employee domain storage."""

from __future__ import annotations

import base64
import json
import os
import sqlite3

import pytest

from hermes_cli.channel_identity import (
    ChannelCrypto,
    BuiltinEmployeeProtected,
    BuiltinAssistantPolicyUnavailable,
    ChannelIdentityStore,
    EmployeeProfileRevisionConflict,
    FeishuCredentialRevisionConflict,
    Keyring,
    create_employee,
    employee_profile_fingerprint,
    ensure_owner_binding,
    list_employees,
    reconcile_employee_workspaces,
    register_employee_feishu_binding,
    resolve_builtin_assistant_personalization,
    resolve_builtin_assistant_policy,
    resolve_employee,
    resolve_employee_feishu_credentials,
    resolve_employee_profile,
    rotate_employee_feishu_credentials,
    set_employee_feishu_binding_status,
    set_employee_status,
    update_builtin_assistant_personalization,
    update_builtin_assistant_policy,
    update_employee_collaboration_policy,
    update_employee_profile,
)
from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.owner_context import owner_context_from_session


def _policy(name="Analyst"):
    return {
        "schema_version": 1,
        "name": name,
        "role": "Research analyst",
        "model_registration_id": "registration-a",
        "system_prompt": "Research carefully.",
        "toolsets": [],
        "skills": [],
        "mcp_servers": [],
        "knowledge_relative_paths": [],
        "max_iterations": 20,
    }


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
    assert version == "17"
    assert {
        "builtin_assistant_policy",
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


def test_builtin_assistant_is_deterministic_self_healing_and_protected(store):
    owner = _owner("owner-a")
    canonical_user_id = ensure_owner_binding(store, owner)
    first = list_employees(store, owner=owner)[0]

    assert first.employee_id.startswith("emp_builtin_")
    assert first.canonical_user_id == canonical_user_id
    assert first.employee_kind == "builtin_assistant"
    assert first.lifecycle_status == "active"
    assert first.protected is True
    assert first.chat_eligible is True
    assert first.profile_revision == 1
    assert first.profile_fingerprint is not None
    assert first.collaboration_policy.may_participate is True
    assert first.collaboration_policy.may_create_groups is True
    assert first.collaboration_policy.invite_quota is None

    with store.write() as conn:
        conn.execute("DROP TRIGGER builtin_assistant_delete_protected")
        conn.execute(
            "DELETE FROM employee_profiles WHERE employee_id=?",
            (first.employee_id,),
        )
        conn.execute("DELETE FROM employees WHERE employee_id=?", (first.employee_id,))
        ChannelIdentityStore._execute_schema(conn)
    repaired = list_employees(store, owner=owner)[0]
    assert repaired.employee_id == first.employee_id

    mutations = (
        lambda: set_employee_status(
            store, owner=owner, employee_id=first.employee_id, status="suspended"
        ),
        lambda: update_employee_profile(
            store,
            owner=owner,
            employee_id=first.employee_id,
            profile={"name": "changed"},
            expected_revision=0,
        ),
        lambda: update_employee_collaboration_policy(
            store,
            owner=owner,
            employee_id=first.employee_id,
            may_participate=False,
            may_create_groups=False,
            invite_quota=0,
        ),
        lambda: register_employee_feishu_binding(
            store,
            owner=owner,
            employee_id=first.employee_id,
            provider_account_id="builtin-app",
            credentials={"app_id": "builtin-app", "app_secret": "secret"},
        ),
    )
    for mutate in mutations:
        with pytest.raises(BuiltinEmployeeProtected):
            mutate()

    from hermes_cli.channel_identity.employee_avatars import (
        delete_employee_avatar,
        save_employee_avatar,
    )

    with pytest.raises(ValueError, match="avatar is invalid"):
        save_employee_avatar(store, first.employee_id, b"invalid")
    assert delete_employee_avatar(store, first.employee_id) is False

    with pytest.raises(sqlite3.IntegrityError, match="builtin employee is protected"):
        with store.write() as conn:
            conn.execute(
                "UPDATE employees SET lifecycle_status='suspended' WHERE employee_id=?",
                (first.employee_id,),
            )
    with pytest.raises(sqlite3.IntegrityError, match="builtin employee is protected"):
        with store.write() as conn:
            conn.execute("DELETE FROM employees WHERE employee_id=?", (first.employee_id,))


def test_global_policy_has_no_owner_fallback_and_personalization_is_scoped(
    store, monkeypatch
):
    owner = _owner("owner-a")
    other = _owner("owner-b")
    builtin = list_employees(store, owner=owner)[0]
    other_builtin = list_employees(store, owner=other)[0]

    with pytest.raises(BuiltinAssistantPolicyUnavailable):
        resolve_builtin_assistant_policy(store)

    monkeypatch.setattr(
        "hermes_cli.model_registrations.resolve_admin_chat_model_registration",
        lambda registration_id: {
            "registration_id": registration_id,
            "provider": "deployment-provider",
            "model": "deployment-model",
            "selection_source": "deployment",
        },
    )
    policy = update_builtin_assistant_policy(
        store,
        model_registration_id="admin-chat-a",
        reasoning_effort="high",
        expected_revision=0,
        updated_by_account_id="account-admin",
    )
    assert policy.model_registration_id == "admin-chat-a"
    assert policy.revision == 1
    assert resolve_builtin_assistant_policy(store).reasoning_effort == "high"

    default_profile = resolve_builtin_assistant_personalization(
        store, owner=owner, employee_id=builtin.employee_id
    )
    assert default_profile.revision == 1
    assert default_profile.profile == {
        "schema_version": 1,
        "nickname": "AI 助手",
        "personal_preference": "",
    }
    personalized = update_builtin_assistant_personalization(
        store,
        owner=owner,
        employee_id=builtin.employee_id,
        expected_revision=1,
        nickname="小助手",
        personal_preference="请用中文简洁回答。",
    )
    assert personalized.revision == 2
    assert personalized.profile == {
        "schema_version": 1,
        "nickname": "小助手",
        "personal_preference": "请用中文简洁回答。",
    }
    other_profile = resolve_builtin_assistant_personalization(
        store, owner=other, employee_id=other_builtin.employee_id
    )
    assert other_profile.profile == {
        "schema_version": 1,
        "nickname": "AI 助手",
        "personal_preference": "",
    }
    with pytest.raises(RuntimeError, match="unavailable"):
        resolve_builtin_assistant_personalization(
            store, owner=other, employee_id=builtin.employee_id
        )
    with pytest.raises(TypeError):
        update_builtin_assistant_personalization(
            store,
            owner=owner,
            employee_id=builtin.employee_id,
            expected_revision=2,
            nickname="Nope",
            personal_preference="",
            system_prompt="override",
        )


def test_v15_upgrade_backfills_builtin_for_existing_owner(
    tmp_path, crypto, monkeypatch
):
    monkeypatch.setenv("HERMES_OWNER_SECRET", "owner-secret")
    first = ChannelIdentityStore(crypto, tmp_path / "control", global_home=tmp_path)
    owner = _owner("owner-a")
    canonical_user_id = ensure_owner_binding(first, owner)
    with first.write() as conn:
        conn.execute("DROP TRIGGER builtin_assistant_delete_protected")
        conn.execute("DROP TRIGGER builtin_assistant_identity_protected")
        conn.execute("DROP INDEX idx_employees_builtin_assistant_owner")
        conn.execute(
            "DELETE FROM employee_profiles WHERE employee_id IN "
            "(SELECT employee_id FROM employees WHERE employee_kind='builtin_assistant')"
        )
        conn.execute("DELETE FROM employees WHERE employee_kind='builtin_assistant'")
        conn.execute(
            "UPDATE channel_identity_meta SET value='15' WHERE key='schema_version'"
        )

    migrated = ChannelIdentityStore(
        crypto, tmp_path / "control", global_home=tmp_path
    )
    employees = list_employees(migrated, owner=owner)
    assert len(employees) == 1
    assert employees[0].canonical_user_id == canonical_user_id
    assert employees[0].employee_kind == "builtin_assistant"


def test_employee_crud_profiles_policy_and_owner_security(store):
    owner = _owner("owner-a")
    other = _owner("owner-b")
    employee = create_employee(store, owner=owner, profile=_policy())

    assert employee.employee_id.startswith("emp_")
    assert employee.feishu_binding is None
    assert employee.collaboration_policy.employee_id == employee.employee_id
    employees = list_employees(store, owner=owner)
    assert {item.employee_kind for item in employees} == {
        "builtin_assistant",
        "managed",
    }
    assert next(
        item.employee_id for item in employees if item.employee_kind == "managed"
    ) == employee.employee_id
    other_employees = list_employees(store, owner=other)
    assert len(other_employees) == 1
    assert other_employees[0].employee_kind == "builtin_assistant"
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
        profile=_policy("Senior Analyst"),
        expected_revision=1,
    )
    assert updated.revision == 2
    with pytest.raises(EmployeeProfileRevisionConflict):
        update_employee_profile(
            store,
            owner=owner,
            employee_id=employee.employee_id,
            profile=_policy("stale"),
            expected_revision=1,
        )
    policy = update_employee_collaboration_policy(
        store,
        owner=owner,
        employee_id=employee.employee_id,
        may_participate=False,
        may_create_groups=True,
        may_create_scheduled_tasks=True,
        invite_quota=None,
    )
    assert policy.employee_id == employee.employee_id
    assert policy.may_create_scheduled_tasks is True
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
        assert other_canonical_user_id is not None
    with pytest.raises(sqlite3.IntegrityError, match="Owner is immutable"):
        with store.write() as conn:
            conn.execute(
                "UPDATE employees SET canonical_user_id='attacker' WHERE employee_id=?",
                (employee.employee_id,),
            )


def test_employee_workspace_is_unique_stable_and_server_managed(store):
    owner = _owner("owner-a")
    first = create_employee(store, owner=owner, profile=_policy("First"))
    second = create_employee(store, owner=owner, profile=_policy("Second"))
    first_profile = resolve_employee_profile(
        store, owner=owner, employee_id=first.employee_id
    )
    second_profile = resolve_employee_profile(
        store, owner=owner, employee_id=second.employee_id
    )

    first_workspace = f"employees/{first.employee_id}"
    second_workspace = f"employees/{second.employee_id}"
    assert first_profile.profile["workspace_relative_path"] == first_workspace
    assert second_profile.profile["workspace_relative_path"] == second_workspace
    assert first_workspace != second_workspace
    assert (owner.host_owner_home / "workspaces" / "default" / first_workspace).is_dir()
    assert (owner.host_owner_home / "workspaces" / "default" / second_workspace).is_dir()

    updated = update_employee_profile(
        store,
        owner=owner,
        employee_id=first.employee_id,
        profile=_policy("Updated"),
        expected_revision=1,
    )
    assert updated.profile["workspace_relative_path"] == first_workspace
    with pytest.raises(ValueError, match="server-managed"):
        update_employee_profile(
            store,
            owner=owner,
            employee_id=first.employee_id,
            profile={**_policy("Moved"), "workspace_relative_path": "employees/moved"},
            expected_revision=2,
        )


def test_employee_workspace_rejects_unsafe_existing_targets(store):
    owner = _owner("owner-a")
    employees = owner.host_owner_home / "workspaces" / "default" / "employees"
    employees.mkdir(mode=0o700, parents=True)
    if os.name != "nt":
        owner.host_owner_home.chmod(0o700)

    file_employee_id = "emp_existing_file"
    (employees / file_employee_id).write_text("not a directory", encoding="utf-8")
    with pytest.raises(RuntimeError, match="must be a directory"):
        create_employee(
            store,
            owner=owner,
            employee_id=file_employee_id,
            profile=_policy(),
        )

    if os.name != "nt":
        symlink_employee_id = "emp_existing_symlink"
        (employees / symlink_employee_id).symlink_to(employees)
        with pytest.raises(RuntimeError, match="must be a directory"):
            create_employee(
                store,
                owner=owner,
                employee_id=symlink_employee_id,
                profile=_policy(),
            )


def test_employee_workspace_is_removed_when_database_commit_fails(store, monkeypatch):
    owner = _owner("owner-a")
    employee_id = "emp_failed_commit"

    def fail_profile_insert(*_args, **_kwargs):
        raise sqlite3.IntegrityError("forced profile failure")

    monkeypatch.setattr(
        "hermes_cli.channel_identity.employees._insert_profile_revision",
        fail_profile_insert,
    )

    with pytest.raises(sqlite3.IntegrityError, match="forced profile failure"):
        create_employee(
            store,
            owner=owner,
            employee_id=employee_id,
            profile=_policy(),
        )

    workspace = owner.host_owner_home / "workspaces" / "default" / "employees" / employee_id
    assert not workspace.exists()
    with store.read() as conn:
        assert conn.execute(
            "SELECT 1 FROM employees WHERE employee_id=?",
            (employee_id,),
        ).fetchone() is None


def test_reconcile_legacy_workspace_once_and_preserve_old_revision(store):
    owner = _owner("owner-a")
    employee = create_employee(store, owner=owner, profile=_policy())
    legacy_profile = {
        **_policy(),
        "workspace_relative_path": "employees/new-employee",
    }
    legacy_payload = json.dumps(
        legacy_profile,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    legacy_fingerprint = employee_profile_fingerprint(legacy_profile)
    ciphertext, key_version = store.crypto.encrypt_text(
        legacy_payload,
        table="employee_profiles",
        record_id=f"{employee.employee_id}:1",
        field="profile",
    )
    with store.write() as conn:
        conn.execute("DROP TRIGGER employee_profiles_identity_immutable")
        conn.execute(
            "UPDATE employee_profiles SET profile_ciphertext=?, profile_key_version=?, "
            "profile_fingerprint=? WHERE employee_id=? AND revision=1",
            (ciphertext, key_version, legacy_fingerprint, employee.employee_id),
        )
        ChannelIdentityStore._execute_schema(conn)

    assert reconcile_employee_workspaces(store) == 1
    assert reconcile_employee_workspaces(store) == 0
    current = resolve_employee_profile(
        store, owner=owner, employee_id=employee.employee_id
    )
    historical = resolve_employee_profile(
        store, owner=owner, employee_id=employee.employee_id, revision=1
    )
    assert current.revision == 2
    assert current.profile["workspace_relative_path"] == f"employees/{employee.employee_id}"
    assert historical.fingerprint == legacy_fingerprint
    assert historical.profile["workspace_relative_path"] == "employees/new-employee"
    assert (
        owner.host_owner_home
        / "workspaces"
        / "default"
        / "employees"
        / employee.employee_id
    ).is_dir()


def test_feishu_binding_lifecycle_credentials_and_rebind_are_separate(store):
    owner = _owner("owner-a")
    employee = create_employee(store, owner=owner, profile=_policy())
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
    create_employee(store, owner=_owner("owner-a"), profile=_policy())

    missing_key_crypto = ChannelCrypto(
        lookup=Keyring(keys={1: b"l" * 32}, active_version=1),
        encryption=Keyring(keys={1: b"e" * 32}, active_version=1),
    )
    with pytest.raises(RuntimeError, match="required key version 2 is unavailable"):
        ChannelIdentityStore(
            missing_key_crypto, tmp_path / "control", global_home=tmp_path
        )
