"""Focused tests for controlled durable dashboard user management."""
from __future__ import annotations

from argparse import Namespace
import pytest

from hermes_cli.dashboard_auth.local_users import LocalUserStore
from hermes_cli.dashboard_users import cmd_dashboard_users


_SECRET = b"s" * 32


def _store(tmp_path) -> LocalUserStore:
    return LocalUserStore(
        secret=_SECRET, control_home=tmp_path / "control", max_accounts=5
    )


def _args(action: str, **kwargs) -> Namespace:
    kwargs.setdefault("json", False)
    return Namespace(dashboard_users_action=action, **kwargs)


@pytest.fixture
def configured_store(monkeypatch, tmp_path):
    store = _store(tmp_path)
    monkeypatch.setattr("hermes_cli.dashboard_users._store", lambda: store)
    return store


def test_list_returns_only_safe_account_metadata(configured_store, capsys):
    configured_store.create_account(username="alice", password="password-secret")

    cmd_dashboard_users(_args("list", json=True))

    output = capsys.readouterr().out
    assert '"username": "alice"' in output
    assert "password-secret" not in output
    assert "password_hash" not in output
    assert "access_token" not in output
    assert "refresh_token" not in output


def test_reset_password_reads_stdin_and_never_echoes_password(
    configured_store, monkeypatch, capsys
):
    account = configured_store.create_account(username="alice", password="old-password")
    session = configured_store.create_session(
        account=account, access_ttl_seconds=60, refresh_ttl_seconds=600
    )
    monkeypatch.setattr("hermes_cli.dashboard_users._read_password", lambda: "new-password")

    cmd_dashboard_users(_args("reset-password", username="alice", require_reset=False))

    captured = capsys.readouterr()
    assert "new-password" not in captured.out
    assert "new-password" not in captured.err
    assert configured_store.verify_credentials(username="alice", password="new-password")
    assert configured_store.verify_access_token(session.access_token) is None


def test_disable_enable_and_revoke_sessions(configured_store, capsys):
    account = configured_store.create_account(username="alice", password="password")
    first = configured_store.create_session(
        account=account, access_ttl_seconds=60, refresh_ttl_seconds=600
    )

    cmd_dashboard_users(_args("disable", username="alice"))
    assert configured_store.get_account("alice").status == "disabled"
    assert configured_store.verify_access_token(first.access_token) is None

    cmd_dashboard_users(_args("enable", username="alice"))
    active = configured_store.get_account("alice")
    assert active.status == "active"
    second = configured_store.create_session(
        account=active, access_ttl_seconds=60, refresh_ttl_seconds=600
    )
    cmd_dashboard_users(_args("revoke-sessions", username="alice"))
    assert configured_store.get_account("alice").status == "active"
    assert configured_store.verify_access_token(second.access_token) is None
    assert "password" not in capsys.readouterr().out


def test_generated_bootstrap_is_tty_only(monkeypatch, capsys):
    monkeypatch.setattr("hermes_cli.dashboard_users._store", lambda: pytest.fail("must not open store"))

    with pytest.raises(SystemExit, match="1"):
        cmd_dashboard_users(_args("bootstrap", generate=True))

    assert "requires an interactive TTY" in capsys.readouterr().err


def test_bootstrap_configures_durable_authority_then_creates_exactly_five(
    monkeypatch, tmp_path, capsys
):
    store = _store(tmp_path)
    calls = []
    accounts = [
        (f"user{number}", f"generated-password-{number}", f"User {number}")
        for number in range(1, 6)
    ]
    monkeypatch.setattr("hermes_cli.dashboard_users._bootstrap_store", lambda: (calls.append(True), store)[1])
    monkeypatch.setattr("hermes_cli.dashboard_users._require_tty_for_reveal_once", lambda: None)
    monkeypatch.setattr("hermes_cli.dashboard_users._generated_accounts", lambda: accounts)

    cmd_dashboard_users(_args("bootstrap", generate=True))

    output = capsys.readouterr().out
    assert calls == [True]
    assert len(store.list_accounts()) == 5
    for username, password, _ in accounts:
        assert f"{username}\t{password}" in output
    with pytest.raises(SystemExit):
        cmd_dashboard_users(_args("bootstrap", generate=True))


def test_bootstrap_preflight_does_not_change_existing_local_authority(monkeypatch, tmp_path):
    import hermes_cli.dashboard_users as users

    store = _store(tmp_path)
    store.create_account(username="alice", password="password")
    monkeypatch.setattr(users, "_store", lambda: store)
    monkeypatch.setattr(users, "_configure_durable_store", lambda: pytest.fail("must not reconfigure"))

    with pytest.raises(Exception, match="already initialized"):
        users._bootstrap_store()


def test_store_configuration_rejects_nonlocal_mode(monkeypatch):
    import hermes_cli.dashboard_users as users

    monkeypatch.setattr(
        "plugins.dashboard_auth.basic._load_config_basic_auth_section",
        lambda: {"store": "remote"},
    )
    with pytest.raises(Exception, match="not configured"):
        users._store()
