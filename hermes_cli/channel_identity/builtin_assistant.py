"""Global execution policy and constrained Owner personalization for the built-in assistant."""

from __future__ import annotations

import time
from dataclasses import dataclass

from hermes_cli.dashboard_auth.owner_context import OwnerContext
from hermes_constants import SELECTABLE_REASONING_LEVELS

from .employees import (
    _append_profile_revision,
    _canonical_profile,
    _insert_profile_revision,
    _owned_employee_row,
    resolve_employee_profile,
)
from .store import ChannelIdentityStore

DEFAULT_BUILTIN_ASSISTANT_NICKNAME = "AI 助手"
_BUILTIN_ASSISTANT_PERSONALIZATION_SCHEMA_VERSION = 1
_MAX_NICKNAME_CHARS = 80
_MAX_PERSONAL_PREFERENCE_CHARS = 20_000


@dataclass(frozen=True)
class BuiltinAssistantPolicy:
    model_registration_id: str
    reasoning_effort: str
    revision: int
    updated_by_account_id: str
    created_at: float
    updated_at: float


class BuiltinAssistantPolicyUnavailable(RuntimeError):
    """The administrator has not configured the global built-in policy."""


class BuiltinAssistantPolicyRevisionConflict(RuntimeError):
    """The requested global policy revision is no longer current."""


def resolve_builtin_assistant_policy(store: ChannelIdentityStore) -> BuiltinAssistantPolicy:
    """Return the singleton global execution policy without an Owner fallback."""
    with store.read() as conn:
        row = conn.execute(
            "SELECT * FROM builtin_assistant_policy WHERE singleton=1"
        ).fetchone()
    if row is None:
        raise BuiltinAssistantPolicyUnavailable(
            "built-in assistant global policy is unavailable"
        )
    return BuiltinAssistantPolicy(
        model_registration_id=row["model_registration_id"],
        reasoning_effort=row["reasoning_effort"],
        revision=int(row["revision"]),
        updated_by_account_id=row["updated_by_account_id"],
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def update_builtin_assistant_policy(
    store: ChannelIdentityStore,
    *,
    model_registration_id: str,
    reasoning_effort: str,
    expected_revision: int,
    updated_by_account_id: str,
) -> BuiltinAssistantPolicy:
    """Replace the singleton policy behind an optimistic revision fence."""
    from hermes_cli.model_registrations import resolve_admin_chat_model_registration

    registration_id = str(model_registration_id or "").strip()
    resolve_admin_chat_model_registration(registration_id)
    reasoning = str(reasoning_effort or "").strip().lower()
    if reasoning not in SELECTABLE_REASONING_LEVELS:
        raise ValueError("reasoning_effort is invalid")
    try:
        expected = int(expected_revision)
    except (TypeError, ValueError) as exc:
        raise ValueError("expected revision must be an integer") from exc
    if expected < 0:
        raise ValueError("expected revision must not be negative")
    account_id = str(updated_by_account_id or "").strip()
    if not account_id:
        raise ValueError("administrator account is required")
    now = time.time()
    with store.write() as conn:
        current = conn.execute(
            "SELECT revision FROM builtin_assistant_policy WHERE singleton=1"
        ).fetchone()
        current_revision = int(current["revision"]) if current is not None else 0
        if current_revision != expected:
            raise BuiltinAssistantPolicyRevisionConflict(
                f"built-in assistant policy revision changed from "
                f"{expected} to {current_revision}"
            )
        revision = current_revision + 1
        conn.execute(
            """
            INSERT INTO builtin_assistant_policy
              (singleton, model_registration_id, reasoning_effort, revision,
               updated_by_account_id, created_at, updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                model_registration_id=excluded.model_registration_id,
                reasoning_effort=excluded.reasoning_effort,
                revision=excluded.revision,
                updated_by_account_id=excluded.updated_by_account_id,
                updated_at=excluded.updated_at
            """,
            (registration_id, reasoning, revision, account_id, now, now),
        )
    return resolve_builtin_assistant_policy(store)


def normalize_builtin_assistant_personalization(profile) -> dict[str, object]:
    """Validate the encrypted built-in personalization profile."""
    if not isinstance(profile, dict):
        raise ValueError("built-in assistant personalization must be an object")
    if set(profile) != {"schema_version", "nickname", "personal_preference"}:
        raise ValueError("built-in assistant personalization fields are invalid")
    if profile.get("schema_version") != _BUILTIN_ASSISTANT_PERSONALIZATION_SCHEMA_VERSION:
        raise ValueError("built-in assistant personalization schema version is invalid")
    nickname = str(profile.get("nickname") or "").strip()
    preference = str(profile.get("personal_preference") or "").strip()
    if not nickname:
        nickname = DEFAULT_BUILTIN_ASSISTANT_NICKNAME
    if len(nickname) > _MAX_NICKNAME_CHARS:
        raise ValueError("built-in assistant nickname is too large")
    if len(preference) > _MAX_PERSONAL_PREFERENCE_CHARS:
        raise ValueError("personal_preference is too large")
    return {
        "schema_version": _BUILTIN_ASSISTANT_PERSONALIZATION_SCHEMA_VERSION,
        "nickname": nickname,
        "personal_preference": preference,
    }


def _personalization_profile(
    *, nickname: str = DEFAULT_BUILTIN_ASSISTANT_NICKNAME, personal_preference: str = ""
) -> dict[str, object]:
    return normalize_builtin_assistant_personalization(
        {
            "schema_version": _BUILTIN_ASSISTANT_PERSONALIZATION_SCHEMA_VERSION,
            "nickname": nickname,
            "personal_preference": personal_preference,
        }
    )


def ensure_builtin_assistant_personalization(
    store: ChannelIdentityStore,
    conn,
    *,
    employee_id: str,
    now: float,
) -> None:
    """Self-heal the default encrypted personalization in the caller's transaction."""
    current = conn.execute(
        "SELECT revision FROM employee_profiles "
        "WHERE employee_id=? AND lifecycle_status='active'",
        (employee_id,),
    ).fetchone()
    if current is not None:
        return
    _, payload, fingerprint = _canonical_profile(_personalization_profile())
    _insert_profile_revision(
        store,
        conn,
        employee_id=employee_id,
        revision=1,
        profile_payload=payload,
        profile_fingerprint=fingerprint,
        now=now,
    )


def resolve_builtin_assistant_personalization(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    employee_id: str,
    revision: int | None = None,
):
    """Return current or pinned Owner-scoped personalization."""
    with store.write() as conn:
        employee = _owned_employee_row(conn, owner=owner, employee_id=employee_id)
        if employee is None or employee["employee_kind"] != "builtin_assistant":
            raise RuntimeError("employee is unavailable")
        ensure_builtin_assistant_personalization(
            store, conn, employee_id=employee_id, now=time.time()
        )
    profile = resolve_employee_profile(
        store,
        owner=owner,
        employee_id=employee_id,
        revision=revision,
    )
    normalize_builtin_assistant_personalization(profile.profile)
    return profile


def update_builtin_assistant_personalization(
    store: ChannelIdentityStore,
    *,
    owner: OwnerContext,
    employee_id: str,
    nickname: str,
    personal_preference: str,
    expected_revision: int,
):
    """Append an immutable Owner-scoped personalization revision."""
    normalized = _personalization_profile(
        nickname=nickname,
        personal_preference=personal_preference,
    )
    _, payload, fingerprint = _canonical_profile(normalized)
    try:
        expected = int(expected_revision)
    except (TypeError, ValueError) as exc:
        raise ValueError("expected revision must be an integer") from exc
    if expected < 0:
        raise ValueError("expected revision must not be negative")
    now = time.time()
    with store.write() as conn:
        employee = _owned_employee_row(conn, owner=owner, employee_id=employee_id)
        if employee is None or employee["employee_kind"] != "builtin_assistant":
            raise RuntimeError("employee is unavailable")
        return _append_profile_revision(
            store,
            conn,
            employee_id=employee_id,
            expected_revision=expected,
            normalized_profile=normalized,
            profile_payload=payload,
            fingerprint=fingerprint,
            now=now,
        )


def builtin_assistant_personalization_payload(profile) -> dict[str, str]:
    normalized = normalize_builtin_assistant_personalization(profile.profile)
    return {
        "nickname": str(normalized["nickname"]),
        "personal_preference": str(normalized["personal_preference"]),
    }
