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
    AuthorityAuditReason,
    audit_authority,
    audit_log,
)
from hermes_cli.owner_worker.audit import (
    report_executor_authority_decision,
    report_worker_lifecycle,
)
from hermes_cli.owner_worker.cgroup_v2 import CgroupResourceEvents
from hermes_cli.owner_worker.executor_identity import ExecutorIdentity


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
            reason=AuthorityAuditReason.TICKET_REJECTED,
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
        reason=AuthorityAuditReason.TICKET_REJECTED,
        epoch=4,
        recovery_generation=2,
        worker_generation=7,
        scope_digest="a" * 64,
        credential_digest="b" * 64,
        issuer_digest="c" * 64,
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
        reason=AuthorityAuditReason.PERSISTED_SCOPE_ASSERTION_MISMATCH,
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


def test_executor_resource_audit_is_allowlisted_and_deidentified(tmp_path, monkeypatch):
    control_home = tmp_path / "control-plane"
    control_home.mkdir()
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(control_home))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_owner")
    identity = ExecutorIdentity(
        owner_key="ok1_owner",
        workspace_prefix="default",
        worker_id="worker-a",
        worker_generation=7,
        lease_version=1,
        recovery_generation=0,
        task_id="task-secret",
        session_id="session-secret",
        executor_id="executor-a",
        executor_generation=2,
    )
    report_executor_authority_decision(
        AuthorityAuditEvent.RESOURCE_OBSERVED,
        AuthorityAuditReason.RESOURCE_MEMORY_OOM,
        identity,
        "resource:" + "a" * 64,
        CgroupResourceEvents(
            populated=True,
            frozen=False,
            cpu={"nr_throttled": 2, "throttled_usec": 11},
            memory={"oom": 1, "oom_kill": 1},
            pids={"max": 3},
        ),
    )

    raw = (control_home / "logs" / "authority.log").read_text()
    entry = json.loads(raw)
    assert entry["event"] == "authority_resource_observed"
    assert entry["reason"] == "resource_memory_oom"
    assert entry["worker_generation"] == 7
    assert entry["executor_generation"] == 2
    assert entry["policy_digest"] != "a" * 64
    assert entry["cpu_nr_throttled"] == 2
    assert entry["cpu_throttled_usec"] == 11
    assert entry["memory_oom"] == 1
    assert entry["memory_oom_kill"] == 1
    assert entry["pids_max"] == 3
    for forbidden in ("ok1_owner", "task-secret", "session-secret", "executor-a", "resource:"):
        assert forbidden not in raw


def test_worker_lifecycle_audit_is_deidentified_and_best_effort(tmp_path, monkeypatch):
    control_home = tmp_path / "control-plane"
    control_home.mkdir()
    monkeypatch.setenv("HERMES_CONTROL_HOME", str(control_home))
    monkeypatch.setenv("HERMES_OWNER_KEY", "ok1_owner")

    report_worker_lifecycle(
        AuthorityAuditEvent.PTY_LIFECYCLE,
        AuthorityAuditReason.ADMITTED,
        worker_generation=7,
    )

    entry = json.loads((control_home / "logs" / "authority.log").read_text())
    assert entry == {
        "ts": entry["ts"],
        "event": "authority_pty_lifecycle",
        "correlation_id": entry["correlation_id"],
        "reason": "admitted",
        "audience_class": "browser-ws",
        "worker_generation": 7,
    }
    assert "ok1_owner" not in (control_home / "logs" / "authority.log").read_text()

    monkeypatch.setattr("hermes_cli.owner_worker.audit.audit_authority", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("unavailable")))
    report_worker_lifecycle(
        AuthorityAuditEvent.PTY_LIFECYCLE,
        AuthorityAuditReason.BRIDGE_CLOSED,
        worker_generation=7,
    )


def test_authority_audit_rejects_unknown_reason_and_sensitive_escape_hatch(profile_home):
    with pytest.raises(ValueError, match="reason"):
        audit_authority(
            AuthorityAuditEvent.TICKET_REJECTED,
            correlation_id="d" * 32,
            reason="ticket=secret",  # type: ignore[arg-type]
        )

    audit_authority(
        AuthorityAuditEvent.TICKET_REJECTED,
        correlation_id="a" * 32,
        reason=AuthorityAuditReason.TICKET_REJECTED,
        audience_class="browser-ws",
    )
    raw = (profile_home / "control-plane" / "logs" / "authority.log").read_text()
    assert "ticket_rejected" in raw
    for forbidden in (
        "ticket=secret", "jti-value", "user@example.test", "/api/ws",
        "ok1_owner", "/owner/home", "token=secret", "capability-value",
        "prompt-body", "tool-arguments", "pty-bytes", "exception-detail",
    ):
        assert forbidden not in raw
