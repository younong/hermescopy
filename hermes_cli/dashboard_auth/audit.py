"""Audit logs for dashboard authentication and Control Plane authority events.

General dashboard-auth events retain their profile-aware log location. Authority
records use a separate Control-Plane-only allowlisted sink so an Owner Worker's
``HERMES_HOME`` can never receive ticket/replay security records.
"""
from __future__ import annotations

import datetime as _dt
import enum
import json
import logging
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)
_write_lock = threading.Lock()

# Field names that must never appear in the general log raw. Any kwarg matching
# these is silently dropped.
_REDACTED_FIELDS: frozenset[str] = frozenset({
    "access_token", "refresh_token", "token", "id_token", "code",
    "code_verifier", "state", "ticket", "jti", "cookie", "prompt",
    "session_id", "membership_revision", "owner_key", "secret",
    "Authorization", "authorization",
})


class AuditEvent(enum.Enum):
    """Event types written to dashboard-auth.log."""

    LOGIN_START = "login_start"
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    LOGOUT = "logout"
    REFRESH_SUCCESS = "refresh_success"
    REFRESH_FAILURE = "refresh_failure"
    REVOKE = "revoke"
    SESSION_VERIFY_FAILURE = "session_verify_failure"
    WS_TICKET_MINTED = "ws_ticket_minted"
    WS_TICKET_REJECTED = "ws_ticket_rejected"
    TOKEN_AUTH_SUCCESS = "token_auth_success"
    TOKEN_AUTH_FAILURE = "token_auth_failure"


class AuthorityAuditEvent(enum.Enum):
    """Strictly de-identified Control Plane authorization decisions."""

    AVAILABILITY_FAILURE = "authority_availability_failure"
    REPLAY_CONTINUITY_INVALIDATED = "authority_replay_continuity_invalidated"
    REPLAY_RECOVERY_COMPLETED = "authority_replay_recovery_completed"
    EPOCH_BUMP = "authority_epoch_bump"
    EPOCH_REVOKED = "authority_epoch_revoked"
    TICKET_MINTED = "authority_ticket_minted"
    TICKET_ADMITTED = "authority_ticket_admitted"
    TICKET_REJECTED = "authority_ticket_rejected"
    CAPABILITY_ADMITTED = "authority_capability_admitted"
    CAPABILITY_REJECTED = "authority_capability_rejected"
    SESSION_REVOKED = "authority_session_revoked"
    WORKER_GENERATION = "authority_worker_generation"
    BRIDGE_LIFECYCLE = "authority_bridge_lifecycle"
    PTY_LIFECYCLE = "authority_pty_lifecycle"
    FILESYSTEM_DENIED = "authority_filesystem_denied"
    EXECUTOR_REJECTED = "authority_executor_rejected"
    CREDENTIAL_LIFECYCLE = "authority_credential_lifecycle"
    EGRESS_REJECTED = "authority_egress_rejected"
    RESOURCE_REJECTED = "authority_resource_rejected"
    RESOURCE_OBSERVED = "authority_resource_observed"
    KEY_ROTATION_FAILURE = "authority_key_rotation_failure"
    BRIDGE_CLOSED = "authority_bridge_closed"
    PERSISTED_SCOPE_REJECTED = "persisted_scope_rejected"


class AuthorityAuditReason(enum.Enum):
    """Closed reason vocabulary for :func:`audit_authority`."""

    ADMITTED = "admitted"
    MINTED = "minted"
    UNSUPPORTED_AUDIENCE = "unsupported_audience"
    TICKET_MINT_UNAVAILABLE = "ticket_mint_unavailable"
    TICKET_REJECTED = "ticket_rejected"
    AUTHORITY_UNAVAILABLE = "authority_unavailable"
    SESSION_AUTHORITY_UNAVAILABLE = "session_authority_unavailable"
    INTERNAL_OWNER_INVALID = "internal_owner_invalid"
    INTERNAL_OWNER_CONTEXT_REQUIRED = "internal_owner_context_required"
    LOGOUT = "logout"
    BOOTSTRAP_REJECTED = "bootstrap_rejected"
    GENERATION_ACTIVE = "generation_active"
    GENERATION_START_FAILED = "generation_start_failed"
    GENERATION_DRAINING = "generation_draining"
    GENERATION_TERMINATED = "generation_terminated"
    GENERATION_REVOKED = "generation_revoked"
    BRIDGE_CLOSED = "bridge_closed"
    BRIDGE_UNAVAILABLE = "bridge_unavailable"
    CONTROL_PLANE_FILESYSTEM_FORBIDDEN = "control_plane_filesystem_forbidden"
    RESOURCE_DECISION_UNAVAILABLE = "resource_decision_unavailable"
    RESOURCE_DECISION_INVALID = "resource_decision_invalid"
    RESOURCE_ADMISSION_REJECTED = "resource_admission_rejected"
    RESOURCE_MEMBERSHIP_REJECTED = "resource_membership_rejected"
    RESOURCE_DEADLINE_EXCEEDED = "resource_deadline_exceeded"
    RESOURCE_OUTPUT_EXCEEDED = "resource_output_exceeded"
    RESOURCE_MEMORY_OOM = "resource_memory_oom"
    RESOURCE_PID_LIMIT = "resource_pid_limit"
    RESOURCE_CPU_THROTTLED = "resource_cpu_throttled"
    RESOURCE_CLEANUP_FAILED = "resource_cleanup_failed"
    EGRESS_PROFILE_REJECTED = "egress_profile_rejected"
    EXECUTOR_LEASE_REJECTED = "executor_lease_rejected"
    SANDBOX_REJECTED = "sandbox_rejected"
    SYSCALL_FILTER_REJECTED = "syscall_filter_rejected"
    CREDENTIAL_REJECTED = "credential_rejected"
    CREDENTIAL_REVOKED_EXECUTOR = "credential_revoked_executor"
    CREDENTIAL_REVOKED_GENERATION = "credential_revoked_generation"
    PERSISTED_SCOPE_ASSERTION_MISMATCH = "persisted_scope_assertion_mismatch"


_AUTHORITY_EVENT_REASONS: dict[AuthorityAuditEvent, frozenset[AuthorityAuditReason]] = {
    AuthorityAuditEvent.AVAILABILITY_FAILURE: frozenset({
        AuthorityAuditReason.AUTHORITY_UNAVAILABLE,
        AuthorityAuditReason.SESSION_AUTHORITY_UNAVAILABLE,
        AuthorityAuditReason.TICKET_MINT_UNAVAILABLE,
    }),
    AuthorityAuditEvent.EPOCH_BUMP: frozenset({AuthorityAuditReason.ADMITTED}),
    AuthorityAuditEvent.EPOCH_REVOKED: frozenset({AuthorityAuditReason.LOGOUT}),
    AuthorityAuditEvent.TICKET_MINTED: frozenset({AuthorityAuditReason.MINTED}),
    AuthorityAuditEvent.TICKET_ADMITTED: frozenset({AuthorityAuditReason.ADMITTED}),
    AuthorityAuditEvent.TICKET_REJECTED: frozenset({
        AuthorityAuditReason.UNSUPPORTED_AUDIENCE,
        AuthorityAuditReason.TICKET_REJECTED,
        AuthorityAuditReason.INTERNAL_OWNER_INVALID,
        AuthorityAuditReason.INTERNAL_OWNER_CONTEXT_REQUIRED,
    }),
    AuthorityAuditEvent.CAPABILITY_ADMITTED: frozenset({AuthorityAuditReason.ADMITTED}),
    AuthorityAuditEvent.CAPABILITY_REJECTED: frozenset({
        AuthorityAuditReason.BOOTSTRAP_REJECTED,
        AuthorityAuditReason.CREDENTIAL_REJECTED,
    }),
    AuthorityAuditEvent.SESSION_REVOKED: frozenset({AuthorityAuditReason.LOGOUT}),
    AuthorityAuditEvent.WORKER_GENERATION: frozenset({
        AuthorityAuditReason.GENERATION_ACTIVE,
        AuthorityAuditReason.GENERATION_START_FAILED,
        AuthorityAuditReason.GENERATION_DRAINING,
        AuthorityAuditReason.GENERATION_TERMINATED,
        AuthorityAuditReason.GENERATION_REVOKED,
    }),
    AuthorityAuditEvent.BRIDGE_LIFECYCLE: frozenset({
        AuthorityAuditReason.ADMITTED,
        AuthorityAuditReason.BRIDGE_CLOSED,
        AuthorityAuditReason.BRIDGE_UNAVAILABLE,
    }),
    AuthorityAuditEvent.PTY_LIFECYCLE: frozenset({
        AuthorityAuditReason.ADMITTED,
        AuthorityAuditReason.BRIDGE_CLOSED,
    }),
    AuthorityAuditEvent.FILESYSTEM_DENIED: frozenset({AuthorityAuditReason.CONTROL_PLANE_FILESYSTEM_FORBIDDEN}),
    AuthorityAuditEvent.EXECUTOR_REJECTED: frozenset({
        AuthorityAuditReason.EXECUTOR_LEASE_REJECTED,
        AuthorityAuditReason.SANDBOX_REJECTED,
        AuthorityAuditReason.SYSCALL_FILTER_REJECTED,
    }),
    AuthorityAuditEvent.CREDENTIAL_LIFECYCLE: frozenset({
        AuthorityAuditReason.CREDENTIAL_REJECTED,
        AuthorityAuditReason.CREDENTIAL_REVOKED_EXECUTOR,
        AuthorityAuditReason.CREDENTIAL_REVOKED_GENERATION,
    }),
    AuthorityAuditEvent.EGRESS_REJECTED: frozenset({AuthorityAuditReason.EGRESS_PROFILE_REJECTED}),
    AuthorityAuditEvent.RESOURCE_REJECTED: frozenset({
        AuthorityAuditReason.RESOURCE_DECISION_UNAVAILABLE,
        AuthorityAuditReason.RESOURCE_DECISION_INVALID,
        AuthorityAuditReason.RESOURCE_ADMISSION_REJECTED,
        AuthorityAuditReason.RESOURCE_MEMBERSHIP_REJECTED,
        AuthorityAuditReason.RESOURCE_DEADLINE_EXCEEDED,
        AuthorityAuditReason.RESOURCE_OUTPUT_EXCEEDED,
        AuthorityAuditReason.RESOURCE_MEMORY_OOM,
        AuthorityAuditReason.RESOURCE_PID_LIMIT,
        AuthorityAuditReason.RESOURCE_CLEANUP_FAILED,
    }),
    AuthorityAuditEvent.RESOURCE_OBSERVED: frozenset({
        AuthorityAuditReason.RESOURCE_MEMORY_OOM,
        AuthorityAuditReason.RESOURCE_PID_LIMIT,
        AuthorityAuditReason.RESOURCE_CPU_THROTTLED,
    }),
    AuthorityAuditEvent.BRIDGE_CLOSED: frozenset({AuthorityAuditReason.BRIDGE_CLOSED}),
    AuthorityAuditEvent.PERSISTED_SCOPE_REJECTED: frozenset({AuthorityAuditReason.PERSISTED_SCOPE_ASSERTION_MISMATCH}),
}

_CORRELATION_ID_RE = re.compile(r"^[a-f0-9]{32,64}$")
_DIGEST_RE = re.compile(r"^(?:sha256:)?[a-f0-9]{64}$")


def _resolve_log_path() -> Path:
    """``$HERMES_HOME/logs/dashboard-auth.log`` with standard fallback."""
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    return Path(home) / "logs" / "dashboard-auth.log"


def _write_json_line(path: Path, entry: dict[str, Any], *, label: str) -> None:
    line = json.dumps(entry, separators=(",", ":")) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
    except Exception as exc:
        _log.warning("%s audit log write failed: %s", label, exc)


def audit_log(event: AuditEvent, **fields: Any) -> None:
    """Append a general dashboard-auth event without token-like values."""
    safe_fields = {key: value for key, value in fields.items() if key not in _REDACTED_FIELDS}
    _write_json_line(
        _resolve_log_path(),
        {
            "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "event": event.value,
            **safe_fields,
        },
        label="dashboard-auth",
    )


def new_authority_correlation_id() -> str:
    """Create an opaque server-side correlation value for one authority flow."""
    return secrets.token_hex(16)


def _authority_log_path() -> Path:
    # Local import keeps the generic logger independent from auth startup while
    # making authority records follow the only approved Control Plane resolver.
    from hermes_cli.dashboard_auth.authority import control_plane_home

    return control_plane_home() / "logs" / "authority.log"


def audit_authority(
    event: AuthorityAuditEvent,
    *,
    correlation_id: str,
    reason: AuthorityAuditReason,
    audience_class: str = "browser-ws",
    epoch: int | None = None,
    recovery_generation: int | None = None,
    worker_generation: int | None = None,
    executor_generation: int | None = None,
    sequence: int | None = None,
    scope_digest: str | None = None,
    credential_digest: str | None = None,
    issuer_digest: str | None = None,
    policy_digest: str | None = None,
    cpu_nr_throttled: int | None = None,
    cpu_throttled_usec: int | None = None,
    memory_oom: int | None = None,
    memory_oom_kill: int | None = None,
    pids_max: int | None = None,
) -> None:
    """Write one allowlisted, de-identified Control Plane authority record.

    The helper intentionally has no ``**fields`` escape hatch. Callers must
    map failures to :class:`AuthorityAuditReason`; user-controlled values,
    URLs, identities, credentials, and exception text cannot enter this sink.
    """
    if not isinstance(event, AuthorityAuditEvent):
        raise ValueError("authority audit event is invalid")
    if not isinstance(reason, AuthorityAuditReason) or reason not in _AUTHORITY_EVENT_REASONS.get(event, frozenset()):
        raise ValueError("authority audit reason is invalid")
    correlation_id = str(correlation_id or "").strip()
    if not _CORRELATION_ID_RE.fullmatch(correlation_id):
        raise ValueError("authority audit correlation_id is invalid")
    if audience_class not in {"browser-ws", "owner-persisted-scope", "none"}:
        raise ValueError("authority audit audience class is invalid")

    entry: dict[str, Any] = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "event": event.value,
        "correlation_id": correlation_id,
        "reason": reason.value,
        "audience_class": audience_class,
    }
    for key, value in (
        ("epoch", epoch),
        ("recovery_generation", recovery_generation),
        ("worker_generation", worker_generation),
        ("executor_generation", executor_generation),
        ("sequence", sequence),
        ("cpu_nr_throttled", cpu_nr_throttled),
        ("cpu_throttled_usec", cpu_throttled_usec),
        ("memory_oom", memory_oom),
        ("memory_oom_kill", memory_oom_kill),
        ("pids_max", pids_max),
    ):
        if value is not None:
            normalized = int(value)
            if normalized < 0:
                raise ValueError(f"authority audit {key} is invalid")
            entry[key] = normalized
    for key, value in (
        ("scope_digest", scope_digest),
        ("credential_digest", credential_digest),
        ("issuer_digest", issuer_digest),
        ("policy_digest", policy_digest),
    ):
        if value is not None:
            normalized = str(value).strip()
            if not _DIGEST_RE.fullmatch(normalized):
                raise ValueError(f"authority audit {key} is invalid")
            entry[key] = normalized
    try:
        path = _authority_log_path()
    except Exception as exc:
        _log.warning("authority audit log path unavailable: %s", exc)
        return
    _write_json_line(path, entry, label="authority")
