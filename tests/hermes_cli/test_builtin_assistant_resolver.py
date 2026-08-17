"""Focused built-in assistant global policy resolution tests."""

from __future__ import annotations

from hermes_cli.channel_identity import (
    ChannelCrypto,
    ChannelIdentityStore,
    Keyring,
    list_employees,
    update_builtin_assistant_personalization,
    update_builtin_assistant_policy,
)
from hermes_cli.collaboration.resolver import CollaborationEmployeeResolver
from hermes_cli.dashboard_auth.base import Session
from hermes_cli.dashboard_auth.owner_context import owner_context_from_session


def test_builtin_resolver_composes_global_policy_and_owner_personalization(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_OWNER_SECRET", "owner-secret")
    store = ChannelIdentityStore(
        ChannelCrypto(
            lookup=Keyring(keys={1: b"l" * 32}, active_version=1),
            encryption=Keyring(keys={1: b"e" * 32}, active_version=1),
        ),
        tmp_path / "control-plane",
        global_home=tmp_path,
    )
    owner = owner_context_from_session(
        Session(
            user_id="owner-a",
            email="owner@example.test",
            display_name="Owner",
            org_id="org-a",
            provider="test",
            expires_at=9_999_999_999,
            access_token="access",
            refresh_token="refresh",
        )
    )
    builtin = list_employees(store, owner=owner)[0]
    monkeypatch.setattr(
        "hermes_cli.model_registrations.resolve_admin_chat_model_registration",
        lambda registration_id: {
            "registration_id": registration_id,
            "provider": "deployment-provider",
            "model": "deployment-model",
            "source": "catalog",
            "selection_source": "deployment",
        },
    )
    update_builtin_assistant_policy(
        store,
        model_registration_id="admin-chat-a",
        reasoning_effort="xhigh",
        expected_revision=0,
        updated_by_account_id="admin-account",
    )
    personalization = update_builtin_assistant_personalization(
        store,
        owner=owner,
        employee_id=builtin.employee_id,
        expected_revision=1,
        nickname="工作助手",
        personal_preference="Always answer with a short summary first.",
    )
    monkeypatch.setattr(
        "hermes_cli.collaboration.resolver.resolve_admin_chat_model_registration",
        lambda registration_id: {
            "registration_id": registration_id,
            "provider": "deployment-provider",
            "model": "deployment-model",
            "source": "catalog",
            "selection_source": "deployment",
        },
    )
    resolver = CollaborationEmployeeResolver(
        owner_key=owner.owner_key,
        control_home=tmp_path / "control-plane",
        global_home=tmp_path,
        connector_config={},
        store=store,
    )
    resolved = resolver.resolve_current(builtin.employee_id)

    assert resolved.member.profile_revision == personalization.revision
    assert resolved.member.profile_fingerprint == personalization.fingerprint
    assert resolved.employee_policy["model"]["registration_id"] == "admin-chat-a"
    assert resolved.employee_policy["reasoning_effort"] == "xhigh"
    assert resolved.employee_policy["global_policy_revision"] == 1
    assert resolved.employee_policy["name"] == "工作助手"
    assert resolved.employee_policy["toolsets"] == ["hermes-cli", "project"]
    assert resolved.employee_policy["mcp_servers"] == []
    assert resolved.employee_policy["system_prompt"].endswith(
        "Always answer with a short summary first.\n</owner_personalization>"
    )

    pinned = resolver.resolve_pinned(
        employee_id=builtin.employee_id,
        profile_revision=personalization.revision,
        profile_fingerprint=personalization.fingerprint,
    )
    assert pinned.employee_policy["snapshot_fingerprint"] == resolved.employee_policy[
        "snapshot_fingerprint"
    ]

    legacy_pinned = resolver.resolve_pinned(
        employee_id=builtin.employee_id,
        profile_revision=1,
        profile_fingerprint="builtin-assistant-v1",
    )
    assert legacy_pinned.member.profile_fingerprint == "builtin-assistant-v1"
    assert (
        legacy_pinned.employee_policy["source_profile_fingerprint"]
        != "builtin-assistant-v1"
    )
