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
    TICKET_MINTED = "authority_ticket_minted"
    TICKET_ADMITTED = "authority_ticket_admitted"
    TICKET_REJECTED = "authority_ticket_rejected"
    SESSION_REVOKED = "authority_session_revoked"
    KEY_ROTATION_FAILURE = "authority_key_rotation_failure"
    BRIDGE_CLOSED = "authority_bridge_closed"


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
    reason: str,
    audience_class: str = "browser-ws",
    epoch: int | None = None,
    recovery_generation: int | None = None,
    sequence: int | None = None,
    scope_digest: str | None = None,
    credential_digest: str | None = None,
    issuer_digest: str | None = None,
) -> None:
    """Write one allowlisted, de-identified Control Plane authority record.

    The helper intentionally has no ``**fields`` escape hatch. Callers must
    map exceptions to fixed reason codes rather than serialize user-controlled
    values, URLs, identities, or browser credentials.
    """
    correlation_id = str(correlation_id or "").strip()
    if not correlation_id:
        raise ValueError("authority audit correlation_id is required")
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("authority audit reason is required")
    if audience_class not in {"browser-ws", "none"}:
        raise ValueError("authority audit audience class is invalid")
    entry: dict[str, Any] = {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "event": event.value,
        "correlation_id": correlation_id,
        "reason": reason,
        "audience_class": audience_class,
    }
    for key, value in (
        ("epoch", epoch),
        ("recovery_generation", recovery_generation),
        ("sequence", sequence),
        ("scope_digest", scope_digest),
        ("credential_digest", credential_digest),
        ("issuer_digest", issuer_digest),
    ):
        if value is not None:
            entry[key] = int(value) if key in {"epoch", "recovery_generation", "sequence"} else str(value)
    try:
        path = _authority_log_path()
    except Exception as exc:
        _log.warning("authority audit log path unavailable: %s", exc)
        return
    _write_json_line(path, entry, label="authority")
