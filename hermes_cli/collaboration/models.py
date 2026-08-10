"""Typed records returned by the collaboration persistence store."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CollaborationMemberProfile:
    """Trusted employee profile identity pinned into a membership."""

    employee_id: str
    profile_revision: int
    profile_fingerprint: str


@dataclass(frozen=True)
class CollaborationGroup:
    group_id: str
    owner_key: str
    name: str
    creator_kind: str
    creator_employee_id: str | None
    status: str
    last_sequence: int
    created_at: float
    updated_at: float
    archived_at: float | None


@dataclass(frozen=True)
class CollaborationMembership:
    membership_id: str
    group_id: str
    employee_id: str
    profile_revision: int
    profile_fingerprint: str
    hidden_session_id: str
    stored_session_id: str
    role: str
    join_sequence: int
    leave_sequence: int | None
    created_at: float
    left_at: float | None


@dataclass(frozen=True)
class CollaborationEvent:
    event_id: str
    group_id: str
    sequence: int
    event_kind: str
    actor_kind: str
    actor_employee_id: str | None
    actor_membership_id: str | None
    body: dict[str, Any]
    created_at: float


@dataclass(frozen=True)
class CollaborationTarget:
    target_id: str
    execution_id: str
    turn_id: str
    employee_id: str
    membership_id: str
    join_sequence: int
    snapshot_sequence: int
    status: str
    error: str | None
    result: dict[str, Any] | None
    last_delivered_sequence: int
    active_seconds: float
    active_started_at: float | None
    attempt: int


@dataclass(frozen=True)
class CollaborationTurn:
    turn_id: str
    group_id: str
    event_id: str
    snapshot_sequence: int
    status: str
    targets: tuple[CollaborationTarget, ...]


@dataclass(frozen=True)
class SubmittedOwnerMessage:
    event: CollaborationEvent
    turn: CollaborationTurn | None
