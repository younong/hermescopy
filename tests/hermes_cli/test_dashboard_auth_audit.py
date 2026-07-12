"""Audit log for dashboard-auth events.

Profile-aware location: ``$HERMES_HOME/logs/dashboard-auth.log``.
Format: one JSON object per line. Token-like kwargs are dropped before
serialisation so we never leak refresh tokens or JWTs to disk.
"""
from __future__ import annotations

import json
import pytest

from hermes_cli.dashboard_auth.audit import (
    AuditEvent,
    AuthorityAuditEvent,
    audit_authority,
    audit_log,
)


@pytest.fixture
def profile_home(tmp_path, monkeypatch):
    """Redirect $HERMES_HOME and ~ to a tmp dir for the duration of the test."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Some code paths fall back to Path.home() — patch that too.
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return home


def test_audit_writes_jsonlines(profile_home):
    audit_log(AuditEvent.LOGIN_START, provider="nous", ip="1.2.3.4")
    audit_log(
        AuditEvent.LOGIN_SUCCESS,
        provider="nous", user_id="u1",
        email="a@b.com", ip="1.2.3.4",
    )

    path = profile_home / "logs" / "dashboard-auth.log"
    assert path.exists(), f"audit log not created at {path}"
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2

    second = json.loads(lines[1])
    assert second["event"] == "login_success"
    assert second["provider"] == "nous"
    assert second["user_id"] == "u1"
    assert second["email"] == "a@b.com"
    assert "ts" in second  # ISO-8601 timestamp


def test_audit_redacts_token_like_fields(profile_home):
    audit_log(
        AuditEvent.LOGIN_SUCCESS,
        provider="nous", access_token="should-not-appear",
        refresh_token="also-not", code="not-this", state="nope",
    )
    raw = (profile_home / "logs" / "dashboard-auth.log").read_text()
    for forbidden in ("should-not-appear", "also-not", "not-this", "nope"):
        assert forbidden not in raw, f"token-like value leaked into audit log: {forbidden}"


def test_audit_all_event_types_have_string_values():
    for ev in AuditEvent:
        assert isinstance(ev.value, str)
        assert ev.value


def test_audit_write_failure_does_not_raise(monkeypatch, tmp_path):
    """A broken audit log must not crash auth."""
    # Point HERMES_HOME at a file (not a dir) so mkdir/open will fail.
    broken = tmp_path / "not-a-dir"
    broken.write_text("blocking file")
    monkeypatch.setenv("HERMES_HOME", str(broken))
    # Should NOT raise.
    audit_log(AuditEvent.LOGIN_FAILURE, provider="nous", reason="x")


def test_audit_creates_logs_dir_if_missing(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # logs/ deliberately does not exist
    audit_log(AuditEvent.LOGIN_START, provider="nous")
    assert (home / "logs").is_dir()
    assert (home / "logs" / "dashboard-auth.log").exists()


def test_authority_audit_requires_correlation_id(profile_home):
    with pytest.raises(ValueError, match="correlation_id"):
        audit_authority(
            AuthorityAuditEvent.TICKET_REJECTED,
            correlation_id="",
            reason="ticket_rejected",
        )


def test_authority_audit_is_allowlisted_and_control_plane_only(tmp_path, monkeypatch):
    owner_home = tmp_path / "users" / "ok1_owner"
    owner_home.mkdir(parents=True)
    control_home = tmp_path / "control-plane"
    control_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(owner_home))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_owner")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(control_home))

    audit_authority(
        AuthorityAuditEvent.TICKET_REJECTED,
        correlation_id="f" * 32,
        reason="ticket_rejected",
        epoch=4,
        recovery_generation=2,
        worker_generation=7,
        scope_digest="scope-digest",
        credential_digest="credential-digest",
        issuer_digest="issuer-digest",
    )

    path = control_home / "logs" / "authority.log"
    assert path.exists()
    assert not (owner_home / "logs" / "authority.log").exists()
    entry = json.loads(path.read_text())
    assert set(entry) == {
        "ts", "event", "correlation_id", "reason", "audience_class",
        "epoch", "recovery_generation", "worker_generation", "scope_digest", "credential_digest",
        "issuer_digest",
    }
    raw = path.read_text()
    for forbidden in (str(owner_home), "ok1_owner", "HERMES_HOME"):
        assert forbidden not in raw


def test_persisted_scope_audit_is_allowlisted(tmp_path, monkeypatch):
    control_home = tmp_path / "control-plane"
    control_home.mkdir()
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_owner")
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(control_home))

    audit_authority(
        AuthorityAuditEvent.PERSISTED_SCOPE_REJECTED,
        correlation_id="b" * 32,
        reason="persisted_scope_assertion_mismatch",
        audience_class="owner-persisted-scope",
        worker_generation=7,
    )

    entry = json.loads((control_home / "logs" / "authority.log").read_text())
    assert entry == {
        "ts": entry["ts"],
        "event": "persisted_scope_rejected",
        "correlation_id": "b" * 32,
        "reason": "persisted_scope_assertion_mismatch",
        "audience_class": "owner-persisted-scope",
        "worker_generation": 7,
    }


def test_authority_audit_has_no_sensitive_escape_hatch(profile_home):
    audit_authority(
        AuthorityAuditEvent.TICKET_REJECTED,
        correlation_id="a" * 32,
        reason="ticket_rejected",
        audience_class="browser-ws",
    )
    raw = (profile_home / "control-plane" / "logs" / "authority.log").read_text()
    assert "ticket_rejected" in raw
    for forbidden in ("ticket=secret", "jti-value", "user@example.test", "/api/ws"):
        assert forbidden not in raw
