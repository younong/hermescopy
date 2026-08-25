"""Atomic persistence operations for internal Owner/employee collaboration."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from hermes_cli.history_pagination import (
    DEFAULT_HISTORY_PAGE_SIZE,
    MAX_HISTORY_PAGE_SIZE,
)
from hermes_state import SessionDB

from .models import (
    CollaborationEvent,
    CollaborationGroup,
    CollaborationMemberProfile,
    CollaborationMembership,
    CollaborationMemberSessionGeneration,
    CollaborationTarget,
    CollaborationTurn,
    SubmittedOwnerMessage,
)


def _identifier(value: str, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{label} is required")
    return normalized


def _optional_sequence(value: int | None, label: str, *, minimum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer greater than or equal to {minimum}")
    return value


def _reconciliation_ids(values: Iterable[str], label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"reconcile_{label}_ids must be an array")
    ids = tuple(dict.fromkeys(_identifier(value, f"{label} ID") for value in values))
    if len(ids) > 512:
        raise ValueError(f"reconcile_{label}_ids exceeds 512 entries")
    return ids


def _sql_placeholders(values: tuple[str, ...]) -> str:
    return ",".join("?" for _ in values)


def _json_object(value: Mapping[str, Any] | None) -> str:
    if value is None:
        return "{}"
    if not isinstance(value, Mapping):
        raise ValueError("event body must be an object")
    try:
        return json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("event body must be JSON serializable") from exc


def aggregate_collaboration_turn(conn, turn_id: str, now: float) -> str:
    """Derive one turn's status from all of its target statuses."""
    statuses = [
        str(row["status"])
        for row in conn.execute(
            "SELECT status FROM collaboration_turn_targets WHERE turn_id=?",
            (turn_id,),
        ).fetchall()
    ]
    if not statuses:
        status = "failed"
    elif any(item in {"running", "waiting_approval"} for item in statuses):
        status = "running"
    elif any(item == "queued" for item in statuses):
        status = "queued"
    elif all(item == "completed" for item in statuses):
        status = "completed"
    elif all(item == "cancelled" for item in statuses):
        status = "cancelled"
    elif any(item == "ambiguous" for item in statuses):
        status = "ambiguous"
    elif any(item == "completed" for item in statuses):
        status = "partial"
    else:
        status = "failed"
    conn.execute(
        "UPDATE collaboration_turns SET status=?, updated_at=?, completed_at=? "
        "WHERE turn_id=?",
        (
            status,
            now,
            now
            if status in {
                "completed",
                "partial",
                "failed",
                "ambiguous",
                "cancelled",
            }
            else None,
            turn_id,
        ),
    )
    return status


class CollaborationStore:
    """Owner-scoped facade over collaboration tables in one ``SessionDB``."""

    def __init__(self, db: SessionDB, *, owner_key: str) -> None:
        self.db = db
        self.owner_key = _identifier(owner_key, "owner key")

    def create_group(
        self,
        name: str,
        *,
        members: Iterable[CollaborationMemberProfile] = (),
        creator_kind: str = "owner",
        creator_employee_id: str | None = None,
        group_id: str | None = None,
        client_idempotency_key: str | None = None,
        provision_member=None,
    ) -> CollaborationGroup:
        name = _identifier(name, "group name")
        group_id = _identifier(group_id or f"cg_{uuid.uuid4().hex}", "group ID")
        creator_kind = _identifier(creator_kind, "creator kind")
        if creator_kind not in {"owner", "employee"}:
            raise ValueError("creator kind is invalid")
        if creator_kind == "owner":
            if creator_employee_id is not None:
                raise ValueError("owner-created groups cannot have a creator account")
        else:
            creator_employee_id = _identifier(creator_employee_id, "creator employee ID")
        pinned_members: list[CollaborationMemberProfile] = []
        seen_employees: set[str] = set()
        for member in members:
            pinned = self._trusted_profile(member)
            if pinned.employee_id in seen_employees:
                continue
            seen_employees.add(pinned.employee_id)
            pinned_members.append(pinned)
        idempotency_key = (
            _identifier(client_idempotency_key, "client idempotency key")
            if client_idempotency_key is not None
            else None
        )
        request_json = json.dumps(
            {
                "name": name,
                "members": [member.__dict__ for member in pinned_members],
                "creator_kind": creator_kind,
                "creator_employee_id": creator_employee_id,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        request_fingerprint = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        now = time.time()

        def _write(conn):
            if idempotency_key is not None:
                existing = conn.execute(
                    "SELECT group_id, request_fingerprint FROM collaboration_group_receipts "
                    "WHERE owner_key=? AND idempotency_key=?",
                    (self.owner_key, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if str(existing["request_fingerprint"]) != request_fingerprint:
                        raise RuntimeError("group idempotency key request mismatch")
                    existing_group = self._owned_group_row(conn, str(existing["group_id"]))
                    if existing_group is None:
                        raise RuntimeError("collaboration group receipt is inconsistent")
                    return dict(existing_group), False
            conn.execute(
                """
                INSERT INTO collaboration_groups
                  (group_id, owner_key, name, creator_kind, creator_employee_id,
                   status, last_sequence, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'active', 0, ?, ?)
                """,
                (
                    group_id,
                    self.owner_key,
                    name,
                    creator_kind,
                    creator_employee_id,
                    now,
                    now,
                ),
            )
            self._append_event(
                conn,
                group_id=group_id,
                event_kind="group.created",
                actor_kind="owner",
                body={"name": name},
                now=now,
            )
            for member in pinned_members:
                membership = self._membership(
                    self._add_membership(
                        conn,
                        group_id=group_id,
                        member=member,
                        actor_kind="owner",
                        now=now,
                    )
                )
                if provision_member is not None:
                    provision_member(membership, member)
            if idempotency_key is not None:
                conn.execute(
                    "INSERT INTO collaboration_group_receipts "
                    "(owner_key, idempotency_key, request_fingerprint, group_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        self.owner_key,
                        idempotency_key,
                        request_fingerprint,
                        group_id,
                        now,
                    ),
                )
            return dict(self._owned_group_row(conn, group_id)), True

        row, _created = self.db._execute_write(_write)
        return self._group(row)

    def list_groups(self, *, include_archived: bool = False) -> tuple[CollaborationGroup, ...]:
        status_clause = "" if include_archived else " AND status='active'"
        with self.db._lock:
            rows = self.db._conn.execute(
                "SELECT * FROM collaboration_groups WHERE owner_key=?"
                f"{status_clause} ORDER BY updated_at DESC, group_id",
                (self.owner_key,),
            ).fetchall()
        return tuple(self._group(dict(row)) for row in rows)

    def get_group(self, group_id: str) -> CollaborationGroup:
        group_id = _identifier(group_id, "group ID")
        with self.db._lock:
            row = self._owned_group_row(self.db._conn, group_id)
        if row is None:
            raise RuntimeError("collaboration group is unavailable")
        return self._group(dict(row))

    def archive_group(
        self, group_id: str
    ) -> tuple[CollaborationGroup, tuple[str, ...]]:
        group_id = _identifier(group_id, "group ID")
        now = time.time()

        def _write(conn):
            group = self._owned_group_row(conn, group_id)
            if group is None:
                raise RuntimeError("collaboration group is unavailable")
            live_sessions: tuple[str, ...] = ()
            if group["status"] == "active":
                live_sessions = tuple(
                    str(row["hidden_session_id"])
                    for row in conn.execute(
                        "SELECT DISTINCT COALESCE(sg.hidden_session_id, m.hidden_session_id) "
                        "AS hidden_session_id FROM collaboration_turn_targets tt "
                        "JOIN collaboration_turns t ON t.turn_id=tt.turn_id "
                        "JOIN collaboration_memberships m ON m.membership_id=tt.membership_id "
                        "LEFT JOIN collaboration_member_session_generations sg "
                        "ON sg.membership_id=tt.membership_id "
                        "AND sg.generation=tt.session_generation "
                        "WHERE t.group_id=? AND tt.status IN ('running','waiting_approval')",
                        (group_id,),
                    ).fetchall()
                )
                event = self._append_event(
                    conn,
                    group_id=group_id,
                    event_kind="group.archived",
                    actor_kind="owner",
                    body={},
                    now=now,
                )
                conn.execute(
                    "UPDATE collaboration_approvals SET status='expired', decided_at=?, "
                    "updated_at=? WHERE status='pending' AND target_id IN ("
                    "SELECT tt.target_id FROM collaboration_turn_targets tt "
                    "JOIN collaboration_turns t ON t.turn_id=tt.turn_id WHERE t.group_id=?)",
                    (now, now, group_id),
                )
                conn.execute(
                    "UPDATE collaboration_turn_targets SET status='cancelled', "
                    "error='group archived', active_seconds=active_seconds + CASE "
                    "WHEN active_started_at IS NULL THEN 0 ELSE MAX(0, ? - active_started_at) END, "
                    "active_started_at=NULL, completed_at=?, updated_at=? WHERE turn_id IN ("
                    "SELECT turn_id FROM collaboration_turns WHERE group_id=?) "
                    "AND status IN ('queued','running','waiting_approval')",
                    (now, now, now, group_id),
                )
                turn_ids = conn.execute(
                    "SELECT turn_id FROM collaboration_turns WHERE group_id=?",
                    (group_id,),
                ).fetchall()
                for turn in turn_ids:
                    aggregate_collaboration_turn(conn, str(turn["turn_id"]), now)
                conn.execute(
                    "UPDATE collaboration_tasks SET status='cancelled', updated_at=?, completed_at=? "
                    "WHERE group_id=? AND status IN ('open','claimed')",
                    (now, now, group_id),
                )
                conn.execute(
                    "UPDATE collaboration_discussions SET status='stopped', updated_at=?, "
                    "completed_at=? WHERE group_id=? AND status='active'",
                    (now, now, group_id),
                )
                conn.execute(
                    "UPDATE collaboration_groups SET status='archived', archived_at=?, "
                    "updated_at=? WHERE group_id=? AND status='active'",
                    (now, now, group_id),
                )
                group = self._owned_group_row(conn, group_id)
                if group is None or int(group["last_sequence"]) != int(event["sequence"]):
                    raise RuntimeError("collaboration archive event is inconsistent")
            return dict(group), live_sessions

        group, live_sessions = self.db._execute_write(_write)
        return self._group(group), live_sessions

    def add_membership(
        self,
        group_id: str,
        member: CollaborationMemberProfile,
    ) -> CollaborationMembership:
        group_id = _identifier(group_id, "group ID")
        member = self._trusted_profile(member)
        now = time.time()

        def _write(conn):
            self._require_active_group(conn, group_id)
            return self._add_membership(
                conn,
                group_id=group_id,
                member=member,
                actor_kind="owner",
                now=now,
            )

        return self._membership(self.db._execute_write(_write))

    def remove_membership(self, group_id: str, employee_id: str) -> CollaborationMembership:
        group_id = _identifier(group_id, "group ID")
        employee_id = _identifier(employee_id, "employee ID")
        now = time.time()

        def _write(conn):
            self._require_active_group(conn, group_id)
            row = conn.execute(
                "SELECT * FROM collaboration_memberships "
                "WHERE group_id=? AND employee_id=? AND leave_sequence IS NULL",
                (group_id, employee_id),
            ).fetchone()
            if row is None:
                raise RuntimeError("active collaboration membership is unavailable")
            event = self._append_event(
                conn,
                group_id=group_id,
                event_kind="membership.left",
                actor_kind="owner",
                body={"employee_id": employee_id, "membership_id": row["membership_id"]},
                now=now,
            )
            conn.execute(
                "UPDATE collaboration_memberships SET leave_sequence=?, left_at=? "
                "WHERE membership_id=? AND leave_sequence IS NULL",
                (event["sequence"], now, row["membership_id"]),
            )
            conn.execute(
                "UPDATE collaboration_discussions SET status='stopped', updated_at=?, "
                "completed_at=? WHERE group_id=? AND status='active' AND discussion_id IN ("
                "SELECT r.discussion_id FROM collaboration_discussion_rounds r "
                "JOIN collaboration_turn_targets tt ON tt.turn_id=r.turn_id "
                "WHERE r.round_number=1 AND tt.membership_id=?)",
                (now, now, group_id, row["membership_id"]),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM collaboration_memberships WHERE membership_id=?",
                    (row["membership_id"],),
                ).fetchone()
            )

        return self._membership(self.db._execute_write(_write))

    def update_memberships(
        self,
        group_id: str,
        *,
        requested_employee_ids: Iterable[str],
        additions: Mapping[str, CollaborationMemberProfile],
        provision_member=None,
    ) -> tuple[CollaborationMembership, ...]:
        """Apply one complete membership delta and its durable sessions atomically."""
        group_id = _identifier(group_id, "group ID")
        requested = tuple(
            dict.fromkeys(
                _identifier(employee_id, "employee ID")
                for employee_id in requested_employee_ids
            )
        )
        trusted_additions = {
            _identifier(employee_id, "employee ID"): self._trusted_profile(member)
            for employee_id, member in additions.items()
        }
        now = time.time()

        def _write(conn):
            self._require_active_group(conn, group_id)
            current_rows = conn.execute(
                "SELECT * FROM collaboration_memberships WHERE group_id=? "
                "AND leave_sequence IS NULL",
                (group_id,),
            ).fetchall()
            current = {str(row["employee_id"]): row for row in current_rows}
            requested_set = set(requested)
            if set(trusted_additions) != requested_set - set(current):
                raise RuntimeError("collaboration membership delta is inconsistent")
            for employee_id, row in current.items():
                if employee_id in requested_set:
                    continue
                event = self._append_event(
                    conn,
                    group_id=group_id,
                    event_kind="membership.left",
                    actor_kind="owner",
                    body={
                        "employee_id": employee_id,
                        "membership_id": str(row["membership_id"]),
                    },
                    now=now,
                )
                conn.execute(
                    "UPDATE collaboration_memberships SET leave_sequence=?, left_at=? "
                    "WHERE membership_id=? AND leave_sequence IS NULL",
                    (event["sequence"], now, row["membership_id"]),
                )
                conn.execute(
                    "UPDATE collaboration_discussions SET status='stopped', updated_at=?, "
                    "completed_at=? WHERE group_id=? AND status='active' AND discussion_id IN ("
                    "SELECT r.discussion_id FROM collaboration_discussion_rounds r "
                    "JOIN collaboration_turn_targets tt ON tt.turn_id=r.turn_id "
                    "WHERE r.round_number=1 AND tt.membership_id=?)",
                    (now, now, group_id, row["membership_id"]),
                )
            for employee_id in requested:
                member = trusted_additions.get(employee_id)
                if member is None:
                    continue
                membership = self._membership(
                    self._add_membership(
                        conn,
                        group_id=group_id,
                        member=member,
                        actor_kind="owner",
                        now=now,
                    )
                )
                if provision_member is not None:
                    provision_member(membership, member)
            rows = conn.execute(
                "SELECT * FROM collaboration_memberships WHERE group_id=? "
                "AND leave_sequence IS NULL ORDER BY join_sequence, employee_id",
                (group_id,),
            ).fetchall()
            return tuple(self._membership(dict(row)) for row in rows)

        return self.db._execute_write(_write)

    def create_attachment(
        self,
        group_id: str,
        *,
        filename: str,
        media_type: str,
        size_bytes: int,
        storage_key: str,
        content_sha256: str,
    ) -> dict[str, Any]:
        group_id = _identifier(group_id, "group ID")
        filename = _identifier(filename, "attachment filename")
        media_type = _identifier(media_type, "attachment media type")
        storage_key = _identifier(storage_key, "attachment storage key")
        content_sha256 = _identifier(content_sha256, "attachment digest")
        if len(content_sha256) != 64:
            raise ValueError("attachment digest is invalid")
        now = time.time()
        attachment_id = f"ca_{uuid.uuid4().hex}"

        def _write(conn):
            self._require_active_group(conn, group_id)
            conn.execute(
                "INSERT INTO collaboration_attachments "
                "(attachment_id, group_id, event_id, owner_key, filename, media_type, "
                "size_bytes, storage_key, content_sha256, created_at) "
                "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attachment_id,
                    group_id,
                    self.owner_key,
                    filename,
                    media_type,
                    int(size_bytes),
                    storage_key,
                    content_sha256,
                    now,
                ),
            )
            return dict(
                conn.execute(
                    "SELECT attachment_id, group_id, filename, media_type, size_bytes, "
                    "content_sha256, created_at FROM collaboration_attachments "
                    "WHERE attachment_id=?",
                    (attachment_id,),
                ).fetchone()
            )

        return self.db._execute_write(_write)

    def append_origin_card(
        self,
        group_id: str,
        *,
        task_id: str,
        collaboration_group_id: str,
        title: str,
        text: str,
        status: str,
    ) -> tuple[CollaborationEvent, bool]:
        """Append one idempotent typed origin card without creating turn targets."""
        group_id = _identifier(group_id, "group ID")
        task_id = _identifier(task_id, "task ID")
        collaboration_group_id = _identifier(
            collaboration_group_id, "collaboration group ID"
        )
        title = _identifier(title, "task title")
        text = str(text or "").strip()
        if status not in {"created", "completed"}:
            raise ValueError("collaboration origin card status is invalid")
        body = {
            "task_id": task_id,
            "group_id": collaboration_group_id,
            "title": title,
            "text": text,
            "status": status,
        }
        request_json = json.dumps(
            body, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        request_fingerprint = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        idempotency_key = f"collaboration-origin:{task_id}:{status}"
        now = time.time()

        def _write(conn):
            self._require_active_group(conn, group_id)
            existing = conn.execute(
                "SELECT event_id, request_fingerprint FROM collaboration_message_receipts "
                "WHERE owner_key=? AND idempotency_key=?",
                (self.owner_key, idempotency_key),
            ).fetchone()
            if existing is not None:
                if str(existing["request_fingerprint"]) != request_fingerprint:
                    raise RuntimeError("collaboration origin card replay is inconsistent")
                event = conn.execute(
                    "SELECT * FROM collaboration_events WHERE event_id=?",
                    (existing["event_id"],),
                ).fetchone()
                return self._event(dict(event)), False
            event = self._event(
                self._append_event(
                    conn,
                    group_id=group_id,
                    event_kind="collaboration.origin.card",
                    actor_kind="system",
                    body=body,
                    now=now,
                )
            )
            conn.execute(
                "INSERT INTO collaboration_message_receipts "
                "(owner_key, idempotency_key, request_fingerprint, group_id, "
                "event_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self.owner_key,
                    idempotency_key,
                    request_fingerprint,
                    group_id,
                    event.event_id,
                    now,
                ),
            )
            return event, True

        return self.db._execute_write(_write)

    def submit_owner_message(
        self,
        group_id: str,
        *,
        text: str,
        mentioned_membership_ids: Iterable[str] = (),
        mention_all: bool = False,
        attachment_ids: Iterable[str] = (),
        client_idempotency_key: str | None = None,
        total_rounds: int = 1,
    ) -> SubmittedOwnerMessage:
        group_id = _identifier(group_id, "group ID")
        text = str(text or "").strip()
        if not text:
            raise ValueError("message text is required")
        if type(total_rounds) is not int or not 1 <= total_rounds <= 10:
            raise ValueError("collaboration round count must be between 1 and 10")
        requested = tuple(
            dict.fromkeys(
                _identifier(value, "membership ID")
                for value in mentioned_membership_ids
            )
        )
        if mention_all and requested:
            raise ValueError("mention_all and explicit mentions are mutually exclusive")
        idempotency_key = (
            _identifier(client_idempotency_key, "client idempotency key")
            if client_idempotency_key is not None
            else None
        )
        requested_attachment_ids = tuple(
            dict.fromkeys(
                _identifier(value, "attachment ID") for value in attachment_ids
            )
        )
        try:
            request_json = json.dumps(
                {
                    "group_id": group_id,
                    "text": text,
                    "mentioned_membership_ids": list(requested),
                    "mention_all": bool(mention_all),
                    "attachment_ids": list(requested_attachment_ids),
                    "total_rounds": total_rounds,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("message request must be JSON serializable") from exc
        request_fingerprint = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        now = time.time()

        def _write(conn):
            self._require_active_group(conn, group_id)
            if idempotency_key is not None:
                existing = conn.execute(
                    "SELECT event_id, request_fingerprint FROM collaboration_message_receipts "
                    "WHERE owner_key=? AND idempotency_key=?",
                    (self.owner_key, idempotency_key),
                ).fetchone()
                if existing is not None:
                    if existing["request_fingerprint"] != request_fingerprint:
                        raise RuntimeError("message idempotency key request mismatch")
                    event = self._event(
                        dict(
                            conn.execute(
                                "SELECT * FROM collaboration_events WHERE event_id=?",
                                (existing["event_id"],),
                            ).fetchone()
                        )
                    )
                    turn_row = conn.execute(
                        "SELECT * FROM collaboration_turns WHERE event_id=?",
                        (event.event_id,),
                    ).fetchone()
                    return SubmittedOwnerMessage(
                        event=event,
                        turn=(
                            self._turn(conn, dict(turn_row))
                            if turn_row is not None
                            else None
                        ),
                    )
            active = conn.execute(
                "SELECT * FROM collaboration_memberships "
                "WHERE group_id=? AND leave_sequence IS NULL ORDER BY join_sequence, employee_id",
                (group_id,),
            ).fetchall()
            by_membership = {str(row["membership_id"]): row for row in active}
            if mention_all:
                target_memberships = tuple(by_membership)
            elif requested:
                target_memberships = requested
            else:
                active_by_employee = {
                    str(row["employee_id"]): str(row["membership_id"])
                    for row in active
                }
                default_membership_id = None
                recent_messages = conn.execute(
                    "SELECT event_kind, actor_employee_id, body_json "
                    "FROM collaboration_events WHERE group_id=? "
                    "AND event_kind IN ('message.owner', 'message.employee') "
                    "ORDER BY sequence DESC",
                    (group_id,),
                ).fetchall()
                for message in recent_messages:
                    if message["event_kind"] == "message.employee":
                        default_membership_id = active_by_employee.get(
                            str(message["actor_employee_id"] or "")
                        )
                    else:
                        body = json.loads(message["body_json"])
                        mentions = body.get("mentions")
                        if (
                            not body.get("mention_all")
                            and isinstance(mentions, list)
                            and len(mentions) == 1
                            and str(mentions[0]) in by_membership
                        ):
                            default_membership_id = str(mentions[0])
                    if default_membership_id is not None:
                        break
                default_membership_id = default_membership_id or next(
                    iter(by_membership), None
                )
                target_memberships = (
                    (default_membership_id,) if default_membership_id is not None else ()
                )
            missing = [
                membership_id
                for membership_id in target_memberships
                if membership_id not in by_membership
            ]
            if missing:
                raise RuntimeError("mentioned collaboration member is unavailable")
            targets = tuple(by_membership[membership_id] for membership_id in target_memberships)
            if total_rounds > 1 and not targets:
                raise RuntimeError("multi-round collaboration requires an active member")
            discussion_id = f"cd_{uuid.uuid4().hex}" if total_rounds > 1 else None
            attachment_rows = []
            for attachment_id in requested_attachment_ids:
                attachment = conn.execute(
                    "SELECT * FROM collaboration_attachments WHERE attachment_id=? "
                    "AND group_id=? AND owner_key=? AND event_id IS NULL",
                    (attachment_id, group_id, self.owner_key),
                ).fetchone()
                if attachment is None:
                    raise RuntimeError("collaboration attachment is unavailable")
                attachment_rows.append(attachment)
            event_row = self._append_event(
                conn,
                group_id=group_id,
                event_kind="message.owner",
                actor_kind="owner",
                body={
                    "text": text,
                    "mentions": list(target_memberships),
                    "mention_all": bool(mention_all),
                    **(
                        {
                            "discussion_id": discussion_id,
                            "discussion_round": 1,
                            "total_rounds": total_rounds,
                        }
                        if discussion_id is not None
                        else {}
                    ),
                },
                now=now,
            )
            event = self._event(event_row)
            if idempotency_key is not None:
                conn.execute(
                    "INSERT INTO collaboration_message_receipts "
                    "(owner_key, idempotency_key, request_fingerprint, group_id, "
                    "event_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        self.owner_key,
                        idempotency_key,
                        request_fingerprint,
                        group_id,
                        event.event_id,
                        now,
                    ),
                )
            attachment_ids = tuple(str(row["attachment_id"]) for row in attachment_rows)
            if attachment_ids:
                placeholders = ",".join("?" for _ in attachment_ids)
                conn.execute(
                    f"UPDATE collaboration_attachments SET event_id=? WHERE attachment_id IN ({placeholders})",
                    (event.event_id, *attachment_ids),
                )
            if not targets:
                return SubmittedOwnerMessage(event=event, turn=None)

            turn_id = f"ct_{uuid.uuid4().hex}"
            conn.execute(
                """
                INSERT INTO collaboration_turns
                  (turn_id, group_id, event_id, snapshot_sequence, status,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', ?, ?)
                """,
                (turn_id, group_id, event.event_id, event.sequence, now, now),
            )
            if discussion_id is not None:
                conn.execute(
                    "INSERT INTO collaboration_discussions "
                    "(discussion_id, group_id, owner_event_id, total_rounds, "
                    "status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 'active', ?, ?)",
                    (discussion_id, group_id, event.event_id, total_rounds, now, now),
                )
                conn.execute(
                    "INSERT INTO collaboration_discussion_rounds "
                    "(discussion_id, round_number, turn_id, created_at) "
                    "VALUES (?, 1, ?, ?)",
                    (discussion_id, turn_id, now),
                )
            result_targets = []
            for membership in targets:
                employee_id = str(membership["employee_id"])
                target_id = f"ctt_{uuid.uuid4().hex}"
                execution_id = f"cex_{uuid.uuid4().hex}"
                conn.execute(
                    """
                    INSERT INTO collaboration_turn_targets
                      (target_id, execution_id, turn_id, employee_id, membership_id,
                       session_generation, join_sequence, snapshot_sequence, status,
                       created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (
                        target_id,
                        execution_id,
                        turn_id,
                        employee_id,
                        membership["membership_id"],
                        membership["current_session_generation"],
                        membership["join_sequence"],
                        event.sequence,
                        now,
                        now,
                    ),
                )
                for attachment_id in attachment_ids:
                    conn.execute(
                        """
                        INSERT INTO collaboration_attachment_grants
                          (grant_id, attachment_id, target_id, granted_sequence, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (f"cag_{uuid.uuid4().hex}", attachment_id, target_id, event.sequence, now),
                    )
                result_targets.append(
                    CollaborationTarget(
                        target_id=target_id,
                        execution_id=execution_id,
                        turn_id=turn_id,
                        employee_id=employee_id,
                        membership_id=str(membership["membership_id"]),
                        session_generation=int(membership["current_session_generation"]),
                        join_sequence=int(membership["join_sequence"]),
                        snapshot_sequence=event.sequence,
                        status="queued",
                        error=None,
                        result=None,
                        last_delivered_sequence=0,
                        active_seconds=0,
                        active_started_at=None,
                        attempt=0,
                    )
                )
            return SubmittedOwnerMessage(
                event=event,
                turn=CollaborationTurn(
                    turn_id=turn_id,
                    group_id=group_id,
                    event_id=event.event_id,
                    snapshot_sequence=event.sequence,
                    status="queued",
                    targets=tuple(result_targets),
                ),
            )

        return self.db._execute_write(_write)

    def advance_discussion(
        self, discussion_id: str
    ) -> tuple[CollaborationEvent, CollaborationTurn] | None:
        """Idempotently create the next durable ordinary-group discussion round."""

        discussion_id = _identifier(discussion_id, "discussion ID")
        now = time.time()

        def _write(conn):
            discussion = conn.execute(
                "SELECT d.*, g.status AS group_status FROM collaboration_discussions d "
                "JOIN collaboration_groups g ON g.group_id=d.group_id "
                "WHERE d.discussion_id=? AND g.owner_key=?",
                (discussion_id, self.owner_key),
            ).fetchone()
            if discussion is None:
                raise RuntimeError("collaboration discussion is unavailable")
            current = conn.execute(
                "SELECT r.round_number, t.* FROM collaboration_discussion_rounds r "
                "JOIN collaboration_turns t ON t.turn_id=r.turn_id "
                "WHERE r.discussion_id=? ORDER BY r.round_number DESC LIMIT 1",
                (discussion_id,),
            ).fetchone()
            if current is None:
                raise RuntimeError("collaboration discussion round is inconsistent")
            current_round = int(current["round_number"])
            terminal = {"completed", "partial", "failed", "ambiguous", "cancelled"}
            if str(current["status"]) not in terminal:
                return None
            if str(discussion["status"]) != "active":
                return None
            if (
                str(discussion["group_status"]) != "active"
                or str(current["status"]) != "completed"
                or current_round >= int(discussion["total_rounds"])
            ):
                status = (
                    "completed"
                    if str(current["status"]) == "completed"
                    and current_round >= int(discussion["total_rounds"])
                    else "stopped"
                )
                conn.execute(
                    "UPDATE collaboration_discussions SET status=?, updated_at=?, "
                    "completed_at=? WHERE discussion_id=? AND status='active'",
                    (status, now, now, discussion_id),
                )
                return None
            next_round = current_round + 1
            existing = conn.execute(
                "SELECT t.* FROM collaboration_discussion_rounds r "
                "JOIN collaboration_turns t ON t.turn_id=r.turn_id "
                "WHERE r.discussion_id=? AND r.round_number=?",
                (discussion_id, next_round),
            ).fetchone()
            if existing is not None:
                return None
            first_targets = conn.execute(
                "SELECT tt.target_id, tt.employee_id, tt.membership_id, "
                "tt.join_sequence, m.current_session_generation AS session_generation, "
                "m.leave_sequence "
                "FROM collaboration_discussion_rounds r "
                "JOIN collaboration_turn_targets tt ON tt.turn_id=r.turn_id "
                "JOIN collaboration_memberships m ON m.membership_id=tt.membership_id "
                "WHERE r.discussion_id=? AND r.round_number=1 "
                "ORDER BY tt.created_at, tt.employee_id",
                (discussion_id,),
            ).fetchall()
            if not first_targets or any(row["leave_sequence"] is not None for row in first_targets):
                conn.execute(
                    "UPDATE collaboration_discussions SET status='stopped', updated_at=?, "
                    "completed_at=? WHERE discussion_id=? AND status='active'",
                    (now, now, discussion_id),
                )
                return None
            body = {
                "text": f"Discussion round {next_round} of {discussion['total_rounds']}",
                "mentions": [str(row["membership_id"]) for row in first_targets],
                "mention_all": False,
                "discussion_id": discussion_id,
                "discussion_round": next_round,
                "total_rounds": int(discussion["total_rounds"]),
            }
            event = self._event(
                self._append_event(
                    conn,
                    group_id=str(discussion["group_id"]),
                    event_kind="discussion.round.started",
                    actor_kind="system",
                    body=body,
                    now=now,
                )
            )
            turn_id = f"ct_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO collaboration_turns "
                "(turn_id, group_id, event_id, snapshot_sequence, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                (turn_id, discussion["group_id"], event.event_id, event.sequence, now, now),
            )
            conn.execute(
                "INSERT INTO collaboration_discussion_rounds "
                "(discussion_id, round_number, turn_id, created_at) VALUES (?, ?, ?, ?)",
                (discussion_id, next_round, turn_id, now),
            )
            for first in first_targets:
                target_id = f"ctt_{uuid.uuid4().hex}"
                conn.execute(
                    "INSERT INTO collaboration_turn_targets "
                    "(target_id, execution_id, turn_id, employee_id, membership_id, "
                    "session_generation, join_sequence, snapshot_sequence, status, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                    (
                        target_id,
                        f"cex_{uuid.uuid4().hex}",
                        turn_id,
                        first["employee_id"],
                        first["membership_id"],
                        first["session_generation"],
                        first["join_sequence"],
                        event.sequence,
                        now,
                        now,
                    ),
                )
                grants = conn.execute(
                    "SELECT attachment_id FROM collaboration_attachment_grants "
                    "WHERE target_id=? ORDER BY created_at, attachment_id",
                    (first["target_id"],),
                ).fetchall()
                for grant in grants:
                    conn.execute(
                        "INSERT INTO collaboration_attachment_grants "
                        "(grant_id, attachment_id, target_id, granted_sequence, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            f"cag_{uuid.uuid4().hex}",
                            grant["attachment_id"],
                            target_id,
                            event.sequence,
                            now,
                        ),
                    )
            conn.execute(
                "UPDATE collaboration_discussions SET updated_at=? "
                "WHERE discussion_id=? AND status='active'",
                (now, discussion_id),
            )
            turn = self._turn(
                conn,
                dict(conn.execute("SELECT * FROM collaboration_turns WHERE turn_id=?", (turn_id,)).fetchone()),
            )
            return event, turn

        return self.db._execute_write(_write)

    def reconcile_discussions(
        self,
    ) -> tuple[tuple[CollaborationEvent, CollaborationTurn], ...]:
        """Advance or terminally settle every due ordinary discussion."""

        with self.db._lock:
            ids = [
                str(row["discussion_id"])
                for row in self.db._conn.execute(
                    "SELECT discussion_id FROM collaboration_discussions d "
                    "JOIN collaboration_groups g ON g.group_id=d.group_id "
                    "WHERE g.owner_key=? AND d.status='active' ORDER BY d.created_at",
                    (self.owner_key,),
                ).fetchall()
            ]
        created = []
        for discussion_id in ids:
            result = self.advance_discussion(discussion_id)
            if result is not None:
                created.append(result)
        return tuple(created)

    def create_ai_task(
        self,
        *,
        title: str,
        brief: str,
        creator: CollaborationMemberProfile,
        members: Iterable[CollaborationMemberProfile],
        source_kind: str,
        source_provider: str = "web",
        source_connector_account_id: str | None = None,
        source_binding_id: str | None = None,
        source_conversation_id: str,
        source_thread_id: str = "",
        source_session_id: str | None = None,
        source_group_id: str | None,
        source_event_id: str | None,
        source_task_id: str | None,
        depth: int,
        allowed_attachment_ids: Iterable[str],
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically create one permanent employee-owned group, task, and origin."""
        title = _identifier(title, "task title")
        brief = _identifier(brief, "task brief")
        creator = self._trusted_profile(creator)
        source_kind = _identifier(source_kind, "source kind")
        if source_kind not in {
            "web_direct",
            "web_group",
            "feishu_direct",
            "cron",
        }:
            raise ValueError("collaboration source kind is invalid")
        source_provider = _identifier(source_provider, "source provider")
        expected_provider = (
            "feishu"
            if source_kind == "feishu_direct"
            else "cron"
            if source_kind == "cron"
            else "web"
        )
        if source_provider != expected_provider:
            raise ValueError("collaboration source provider is invalid")
        source_connector_account_id = (
            _identifier(source_connector_account_id, "source connector account ID")
            if source_connector_account_id is not None
            else None
        )
        source_binding_id = (
            _identifier(source_binding_id, "source binding ID")
            if source_binding_id is not None
            else None
        )
        source_conversation_id = _identifier(
            source_conversation_id, "source conversation ID"
        )
        source_thread_id = str(source_thread_id or "")
        source_session_id = (
            _identifier(source_session_id, "source session ID")
            if source_session_id is not None
            else None
        )
        if source_kind == "feishu_direct":
            if source_connector_account_id is None or source_binding_id is None or source_session_id is None:
                raise RuntimeError("trusted Feishu origin identity is incomplete")
        elif source_kind == "cron":
            if source_connector_account_id is not None or source_binding_id is not None:
                raise RuntimeError("cron origin identity is invalid")
            if source_session_id is not None:
                raise RuntimeError("cron source session identity is invalid")
        elif source_connector_account_id is not None or source_binding_id is not None:
            raise RuntimeError("web origin identity is invalid")
        source_group_id = (
            _identifier(source_group_id, "source group ID")
            if source_group_id is not None
            else None
        )
        source_event_id = (
            _identifier(source_event_id, "source event ID")
            if source_event_id is not None
            else None
        )
        source_task_id = (
            _identifier(source_task_id, "source task ID")
            if source_task_id is not None
            else None
        )
        if depth != 1:
            raise ValueError("AI-created collaboration depth must be one")
        idempotency_key = _identifier(idempotency_key, "idempotency key")
        requested_attachment_ids = tuple(
            dict.fromkeys(_identifier(value, "attachment ID") for value in allowed_attachment_ids)
        )
        pinned_members = [creator]
        seen = {creator.employee_id}
        for member in members:
            pinned = self._trusted_profile(member)
            if pinned.employee_id not in seen:
                pinned_members.append(pinned)
                seen.add(pinned.employee_id)
        request_json = json.dumps(
            {
                "title": title,
                "brief": brief,
                "creator": creator.__dict__,
                "members": [member.__dict__ for member in pinned_members],
                "source_kind": source_kind,
                "source_provider": source_provider,
                "source_connector_account_id": source_connector_account_id,
                "source_binding_id": source_binding_id,
                "source_conversation_id": source_conversation_id,
                "source_thread_id": source_thread_id,
                "source_session_id": source_session_id,
                "source_group_id": source_group_id,
                "source_event_id": source_event_id,
                "source_task_id": source_task_id,
                "depth": depth,
                "allowed_attachment_ids": requested_attachment_ids,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        fingerprint = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        now = time.time()

        def _write(conn):
            existing = conn.execute(
                "SELECT result_json, request_json FROM collaboration_agent_receipts "
                "WHERE owner_key=? AND idempotency_key=? AND operation='create'",
                (self.owner_key, idempotency_key),
            ).fetchone()
            if existing is not None:
                if hashlib.sha256(str(existing["request_json"]).encode("utf-8")).hexdigest() != fingerprint:
                    raise RuntimeError("collaboration idempotency key request mismatch")
                return json.loads(str(existing["result_json"])), False
            if source_task_id is not None:
                parent = conn.execute(
                    "SELECT depth FROM collaboration_tasks t JOIN collaboration_groups g "
                    "ON g.group_id=t.group_id WHERE t.task_id=? AND g.owner_key=?",
                    (source_task_id, self.owner_key),
                ).fetchone()
                if parent is None or int(parent["depth"]) >= 1:
                    raise RuntimeError("nested collaboration group creation is unavailable")
            origin_attachments = []
            for attachment_id in requested_attachment_ids:
                if source_kind != "web_group":
                    raise RuntimeError("web direct attachment transfer is unavailable")
                attachment = conn.execute(
                    "SELECT a.* FROM collaboration_attachments a JOIN collaboration_groups g "
                    "ON g.group_id=a.group_id WHERE a.attachment_id=? AND a.group_id=? "
                    "AND g.owner_key=?",
                    (attachment_id, source_group_id, self.owner_key),
                ).fetchone()
                if attachment is None:
                    raise RuntimeError("origin attachment is unavailable")
                origin_attachments.append(attachment)
            group_id = f"cg_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO collaboration_groups "
                "(group_id, owner_key, name, creator_kind, creator_employee_id, status, "
                "last_sequence, created_at, updated_at) VALUES "
                "(?, ?, ?, 'employee', ?, 'active', 0, ?, ?)",
                (group_id, self.owner_key, title, creator.employee_id, now, now),
            )
            created = self._append_event(
                conn,
                group_id=group_id,
                event_kind="group.created",
                actor_kind="system",
                actor_employee_id=creator.employee_id,
                body={"name": title, "brief": brief, "ai_created": True},
                now=now,
            )
            memberships: list[dict[str, Any]] = []
            for member in pinned_members:
                membership = self._add_membership(
                    conn,
                    group_id=group_id,
                    member=member,
                    actor_kind="system",
                    now=now,
                    role="owner" if member.employee_id == creator.employee_id else "member",
                )
                memberships.append(membership)
            creator_membership = memberships[0]
            transferred_attachment_ids = []
            for source_attachment in origin_attachments:
                attachment_id = f"ca_{uuid.uuid4().hex}"
                conn.execute(
                    "INSERT INTO collaboration_attachments "
                    "(attachment_id, group_id, event_id, owner_key, filename, media_type, "
                    "size_bytes, storage_key, content_sha256, created_at) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        attachment_id,
                        group_id,
                        created["event_id"],
                        self.owner_key,
                        source_attachment["filename"],
                        source_attachment["media_type"],
                        source_attachment["size_bytes"],
                        source_attachment["storage_key"],
                        source_attachment["content_sha256"],
                        now,
                    ),
                )
                transferred_attachment_ids.append(attachment_id)
            task_id = f"cat_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO collaboration_tasks "
                "(task_id, group_id, created_event_id, assigned_employee_id, "
                "creator_employee_id, creator_membership_id, creator_profile_revision, "
                "creator_profile_fingerprint, title, description, source_kind, round, "
                "max_rounds, depth, source_event_id, source_task_id, "
                "allowed_attachment_ids_json, status, created_at, updated_at) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 3, ?, ?, ?, ?, 'open', ?, ?)",
                (
                    task_id,
                    group_id,
                    created["event_id"],
                    creator.employee_id,
                    creator.employee_id,
                    creator_membership["membership_id"],
                    creator.profile_revision,
                    creator.profile_fingerprint,
                    title,
                    brief,
                    source_kind,
                    depth,
                    source_event_id,
                    source_task_id,
                    json.dumps(transferred_attachment_ids, separators=(",", ":")),
                    now,
                    now,
                ),
            )
            origin_id = f"cao_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO collaboration_origins "
                "(origin_id, group_id, provider, employee_id, connector_account_id, binding_id, conversation_id, "
                "thread_id, source_kind, source_session_id, source_group_id, source_event_id, "
                "source_task_id, round, depth, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    origin_id,
                    group_id,
                    source_provider,
                    creator.employee_id,
                    source_connector_account_id,
                    source_binding_id,
                    source_conversation_id,
                    source_thread_id,
                    source_kind,
                    source_session_id,
                    source_group_id,
                    source_event_id,
                    source_task_id,
                    depth,
                    now,
                ),
            )
            result = {
                "task_id": task_id,
                "group_id": group_id,
                "origin_id": origin_id,
                "created_event_id": str(created["event_id"]),
                "memberships": memberships,
                "round": 0,
                "status": "open",
                "allowed_attachment_ids": transferred_attachment_ids,
            }
            conn.execute(
                "INSERT INTO collaboration_agent_receipts "
                "(receipt_id, owner_key, operation, idempotency_key, request_json, "
                "result_json, created_at) VALUES (?, ?, 'create', ?, ?, ?, ?)",
                (
                    f"car_{uuid.uuid4().hex}",
                    self.owner_key,
                    idempotency_key,
                    request_json,
                    json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
            return result, True

        return self.db._execute_write(_write)

    def ai_task(self, task_id: str) -> dict[str, Any]:
        task_id = _identifier(task_id, "task ID")
        with self.db._lock:
            row = self.db._conn.execute(
                "SELECT t.*, o.origin_id, o.provider, o.connector_account_id AS origin_connector_account_id, "
                "o.binding_id, o.conversation_id, o.thread_id, o.source_session_id, "
                "o.source_group_id, o.creation_delivery_key, o.completion_delivery_key, "
                "o.creation_delivered_at, o.completion_delivered_at "
                "FROM collaboration_tasks t JOIN collaboration_groups g ON g.group_id=t.group_id "
                "JOIN collaboration_origins o ON o.group_id=t.group_id "
                "WHERE t.task_id=? AND g.owner_key=?",
                (task_id, self.owner_key),
            ).fetchone()
        if row is None:
            raise RuntimeError("collaboration task is unavailable")
        result = dict(row)
        result["allowed_attachment_ids"] = json.loads(
            str(result.pop("allowed_attachment_ids_json") or "[]")
        )
        return result

    def complete_ai_task(
        self,
        task_id: str,
        *,
        summary: str,
        idempotency_key: str,
    ) -> tuple[dict[str, Any], bool]:
        task_id = _identifier(task_id, "task ID")
        summary = _identifier(summary, "summary")
        idempotency_key = _identifier(idempotency_key, "idempotency key")
        request_json = json.dumps(
            {"task_id": task_id, "summary": summary},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        now = time.time()

        def _write(conn):
            task = conn.execute(
                "SELECT t.*, g.owner_key, g.status AS group_status FROM collaboration_tasks t "
                "JOIN collaboration_groups g ON g.group_id=t.group_id "
                "WHERE t.task_id=? AND g.owner_key=?",
                (task_id, self.owner_key),
            ).fetchone()
            if task is None:
                raise RuntimeError("collaboration task is unavailable")
            if task["group_status"] != "active":
                raise RuntimeError("collaboration group is archived")
            receipt = conn.execute(
                "SELECT request_json, result_json FROM collaboration_agent_receipts "
                "WHERE owner_key=? AND operation='finish' AND idempotency_key=?",
                (self.owner_key, idempotency_key),
            ).fetchone()
            if receipt is not None:
                if str(receipt["request_json"]) != request_json:
                    raise RuntimeError("collaboration idempotency key request mismatch")
                return json.loads(str(receipt["result_json"])), False
            if task["status"] == "completed":
                if str(task["summary_text"] or "") != summary:
                    raise RuntimeError("collaboration task is already completed")
                result = {"task_id": task_id, "group_id": str(task["group_id"]), "status": "completed"}
            else:
                active = conn.execute(
                    "SELECT 1 FROM collaboration_turns WHERE group_id=? "
                    "AND status IN ('queued','running') LIMIT 1",
                    (task["group_id"],),
                ).fetchone()
                if active is not None:
                    raise RuntimeError("collaboration round is still running")
                conn.execute(
                    "UPDATE collaboration_tasks SET status='completed', summary_text=?, "
                    "completed_at=?, updated_at=? WHERE task_id=? AND status='open'",
                    (summary, now, now, task_id),
                )
                event = self._append_event(
                    conn,
                    group_id=str(task["group_id"]),
                    event_kind="task.completed",
                    actor_kind="system",
                    body={"task_id": task_id, "summary": summary},
                    now=now,
                )
                result = {
                    "task_id": task_id,
                    "group_id": str(task["group_id"]),
                    "event_id": str(event["event_id"]),
                    "status": "completed",
                }
            conn.execute(
                "INSERT INTO collaboration_agent_receipts "
                "(receipt_id, owner_key, operation, idempotency_key, request_json, "
                "result_json, created_at) VALUES (?, ?, 'finish', ?, ?, ?, ?)",
                (
                    f"car_{uuid.uuid4().hex}",
                    self.owner_key,
                    idempotency_key,
                    request_json,
                    json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
            return result, True

        return self.db._execute_write(_write)

    def mark_ai_task_ambiguous(
        self,
        task_id: str,
        *,
        reason: str,
    ) -> tuple[dict[str, Any], bool]:
        """Terminally record a coordinator outcome that cannot be retried safely."""
        task_id = _identifier(task_id, "task ID")
        reason = _identifier(reason, "ambiguity reason")
        now = time.time()

        def _write(conn):
            task = conn.execute(
                "SELECT t.*, g.status AS group_status FROM collaboration_tasks t "
                "JOIN collaboration_groups g ON g.group_id=t.group_id "
                "WHERE t.task_id=? AND g.owner_key=?",
                (task_id, self.owner_key),
            ).fetchone()
            if task is None:
                raise RuntimeError("collaboration task is unavailable")
            if task["group_status"] != "active":
                raise RuntimeError("collaboration group is archived")
            if task["status"] == "completed":
                return {
                    "task_id": task_id,
                    "group_id": str(task["group_id"]),
                    "status": "completed",
                }, False
            if task["status"] == "ambiguous":
                return {
                    "task_id": task_id,
                    "group_id": str(task["group_id"]),
                    "status": "ambiguous",
                }, False
            changed = conn.execute(
                "UPDATE collaboration_tasks SET status='ambiguous', updated_at=?, "
                "completed_at=? WHERE task_id=? AND status IN ('open','claimed')",
                (now, now, task_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("collaboration task cannot become ambiguous")
            event = self._append_event(
                conn,
                group_id=str(task["group_id"]),
                event_kind="task.ambiguous",
                actor_kind="system",
                body={"task_id": task_id, "reason": reason},
                now=now,
            )
            return {
                "task_id": task_id,
                "group_id": str(task["group_id"]),
                "event_id": str(event["event_id"]),
                "status": "ambiguous",
            }, True

        return self.db._execute_write(_write)

    def ensure_origin_delivery_intent(
        self,
        task_id: str,
        *,
        completion: bool,
        worker_owner_key: str,
        worker_id: str | None = None,
        worker_generation: int | None = None,
        lease_version: int | None = None,
        recovery_generation: int | None = None,
    ) -> dict[str, Any]:
        """Persist one stable Feishu result-return intent before Control Plane handoff."""
        task = self.ai_task(task_id)
        if task["provider"] != "feishu" or task["source_kind"] != "feishu_direct":
            raise RuntimeError("Feishu direct collaboration origin is required")
        kind = "completion" if completion else "creation"
        if completion and task["status"] != "completed":
            raise RuntimeError("collaboration completion is not durable")
        event_id = (
            str(task.get("created_event_id") or "")
            if not completion
            else ""
        )
        if completion:
            with self.db._lock:
                event = self.db._conn.execute(
                    "SELECT event_id FROM collaboration_events WHERE group_id=? "
                    "AND event_kind='task.completed' ORDER BY sequence DESC LIMIT 1",
                    (task["group_id"],),
                ).fetchone()
            event_id = str(event["event_id"] if event is not None else "")
        if not event_id:
            raise RuntimeError("collaboration delivery event is unavailable")
        delivery_key = f"collaboration:{task_id}:{kind}"
        title = str(task["title"])
        if completion:
            payload = (
                f"[Internal collaboration completed]\n"
                f"Group: {title} ({task['group_id']})\n"
                f"Task: {task_id}\nStatus: completed\n"
                f"Summary: {str(task.get('summary_text') or '')}"
            )
        else:
            payload = (
                f"[Internal collaboration created]\n"
                f"Group: {title} ({task['group_id']})\n"
                f"Task: {task_id}\nStatus: {task['status']}"
            )
        now = time.time()

        def _write(conn):
            existing = conn.execute(
                "SELECT * FROM collaboration_delivery_state WHERE delivery_key=?",
                (delivery_key,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["origin_id"]) != str(task["origin_id"])
                    or str(existing["event_id"]) != event_id
                    or str(existing["delivery_kind"]) != kind
                    or str(existing["payload_text"]) != payload
                ):
                    raise RuntimeError("collaboration delivery key request mismatch")
                return dict(existing)
            delivery_id = f"cad_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO collaboration_delivery_state "
                "(delivery_id, event_id, origin_id, delivery_kind, delivery_key, "
                "payload_text, status, next_attempt_at, worker_owner_key, worker_id, "
                "worker_generation, lease_version, recovery_generation, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?, ?, ?, ?, ?)",
                (
                    delivery_id, event_id, task["origin_id"], kind, delivery_key,
                    payload, worker_owner_key, worker_id, worker_generation,
                    lease_version, recovery_generation, now, now,
                ),
            )
            key_field = "completion_delivery_key" if completion else "creation_delivery_key"
            conn.execute(
                f"UPDATE collaboration_origins SET {key_field}=COALESCE({key_field}, ?) "
                "WHERE origin_id=?",
                (delivery_key, task["origin_id"]),
            )
            return dict(conn.execute(
                "SELECT * FROM collaboration_delivery_state WHERE delivery_id=?",
                (delivery_id,),
            ).fetchone())

        return self.db._execute_write(_write)

    def pending_origin_deliveries(
        self,
        *,
        worker_owner_key: str | None = None,
        worker_id: str | None = None,
        worker_generation: int | None = None,
        lease_version: int | None = None,
        recovery_generation: int | None = None,
        limit: int = 128,
    ) -> list[dict[str, Any]]:
        """Adopt due intents onto the current exact fence and return them."""
        exact_owner = str(worker_owner_key or "").strip() or None
        fence = (worker_id, worker_generation, lease_version, recovery_generation)
        if exact_owner is None and any(value is not None for value in fence):
            raise ValueError("complete collaboration delivery fence is required")
        if exact_owner is not None and any(value is None for value in fence):
            raise ValueError("complete collaboration delivery fence is required")
        bounded = max(1, min(int(limit), 128))

        def _write(conn):
            if exact_owner is not None:
                if exact_owner != self.owner_key:
                    raise RuntimeError("collaboration delivery owner mismatch")
                conn.execute(
                    "UPDATE collaboration_delivery_state SET worker_owner_key=?, worker_id=?, "
                    "worker_generation=?, lease_version=?, recovery_generation=?, updated_at=? "
                    "WHERE status IN ('pending','claimed') AND delivery_id IN ("
                    "SELECT d.delivery_id FROM collaboration_delivery_state d "
                    "JOIN collaboration_origins o ON o.origin_id=d.origin_id "
                    "JOIN collaboration_groups g ON g.group_id=o.group_id "
                    "WHERE g.owner_key=? AND d.status IN ('pending','claimed') "
                    "ORDER BY d.created_at LIMIT ?)",
                    (
                        exact_owner,
                        str(worker_id),
                        int(worker_generation),
                        int(lease_version),
                        int(recovery_generation),
                        time.time(),
                        exact_owner,
                        bounded,
                    ),
                )
            rows = conn.execute(
                "SELECT d.*, o.provider, o.employee_id, o.connector_account_id, "
                "o.binding_id, o.conversation_id, o.thread_id, o.source_session_id "
                "FROM collaboration_delivery_state d "
                "JOIN collaboration_origins o ON o.origin_id=d.origin_id "
                "JOIN collaboration_groups g ON g.group_id=o.group_id "
                "WHERE g.owner_key=? AND d.status IN ('pending','claimed') "
                "ORDER BY d.created_at LIMIT ?",
                (self.owner_key, bounded),
            ).fetchall()
            return [dict(row) for row in rows]

        if exact_owner is not None:
            return self.db._execute_write(_write)
        with self.db._lock:
            return _write(self.db._conn)

    def record_origin_delivery_result(
        self,
        delivery_key: str,
        *,
        outbound_id: str | None = None,
        result_status: str | None = None,
        error: str | None = None,
        ambiguous: bool = False,
    ) -> bool:
        delivery_key = _identifier(delivery_key, "delivery key")
        exact_outbound = str(outbound_id or "").strip() or None
        if ambiguous:
            status = "ambiguous"
        elif result_status is not None:
            status = str(result_status).strip()
        elif error:
            status = "failed"
        elif exact_outbound:
            status = "claimed"
        else:
            raise ValueError("collaboration delivery result is incomplete")
        if status not in {"claimed", "delivered", "failed", "ambiguous"}:
            raise ValueError("collaboration delivery result status is invalid")
        if status in {"claimed", "delivered"} and not exact_outbound:
            raise ValueError("collaboration delivery outbound ID is required")
        now = time.time()
        with self.db._lock:
            row = self.db._conn.execute(
                "SELECT d.delivery_id, d.delivery_kind, d.origin_id, d.status, d.outbound_id "
                "FROM collaboration_delivery_state d WHERE d.delivery_key=?",
                (delivery_key,),
            ).fetchone()
            if row is None:
                return False
            existing_outbound = str(row["outbound_id"] or "").strip() or None
            if existing_outbound and exact_outbound and existing_outbound != exact_outbound:
                raise RuntimeError("collaboration delivery outbox receipt changed")
            if row["status"] in {"delivered", "failed", "ambiguous"}:
                return row["status"] == status
            changed = self.db._conn.execute(
                "UPDATE collaboration_delivery_state SET status=?, outbound_id=COALESCE(outbound_id, ?), "
                "last_error=?, updated_at=?, delivered_at=? WHERE delivery_id=? "
                "AND status IN ('pending','claimed')",
                (
                    status, exact_outbound, error, now,
                    now if status == "delivered" else None, row["delivery_id"],
                ),
            ).rowcount
            if changed == 1 and status == "delivered":
                field = (
                    "completion_delivered_at"
                    if row["delivery_kind"] == "completion"
                    else "creation_delivered_at"
                )
                self.db._conn.execute(
                    f"UPDATE collaboration_origins SET {field}=COALESCE({field}, ?) "
                    "WHERE origin_id=?",
                    (now, row["origin_id"]),
                )
            self.db._conn.commit()
        return changed == 1

    def mark_origin_delivered(self, task_id: str, *, completion: bool) -> None:
        task = self.ai_task(task_id)
        field = "completion_delivered_at" if completion else "creation_delivered_at"
        with self.db._lock:
            self.db._conn.execute(
                f"UPDATE collaboration_origins SET {field}=COALESCE({field}, ?) "
                "WHERE origin_id=?",
                (time.time(), task["origin_id"]),
            )
            self.db._conn.commit()

    def dispatch_ai_round(
        self,
        task_id: str,
        *,
        instruction: str,
        target_employee_ids: Iterable[str],
        attachment_ids: Iterable[str],
        idempotency_key: str,
    ) -> tuple[SubmittedOwnerMessage, int, bool]:
        """Atomically append one explicit round, advance the task, and receipt it."""
        task_id = _identifier(task_id, "task ID")
        instruction = _identifier(instruction, "instruction")
        targets = tuple(
            dict.fromkeys(_identifier(value, "target employee ID") for value in target_employee_ids)
        )
        if not targets:
            raise ValueError("target employee IDs are required")
        attachments = tuple(
            dict.fromkeys(_identifier(value, "attachment ID") for value in attachment_ids)
        )
        idempotency_key = _identifier(idempotency_key, "idempotency key")
        request_json = json.dumps(
            {
                "task_id": task_id,
                "instruction": instruction,
                "target_employee_ids": list(targets),
                "attachment_ids": list(attachments),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        now = time.time()

        def _write(conn):
            receipt = conn.execute(
                "SELECT request_json, result_json FROM collaboration_agent_receipts "
                "WHERE owner_key=? AND operation='dispatch' AND idempotency_key=?",
                (self.owner_key, idempotency_key),
            ).fetchone()
            if receipt is not None:
                if str(receipt["request_json"]) != request_json:
                    raise RuntimeError("collaboration idempotency key request mismatch")
                result = json.loads(str(receipt["result_json"]))
                event = self._event(
                    dict(
                        conn.execute(
                            "SELECT * FROM collaboration_events WHERE event_id=?",
                            (result["event_id"],),
                        ).fetchone()
                    )
                )
                turn = self._turn(
                    conn,
                    dict(
                        conn.execute(
                            "SELECT * FROM collaboration_turns WHERE turn_id=?",
                            (result["turn_id"],),
                        ).fetchone()
                    ),
                )
                return SubmittedOwnerMessage(event=event, turn=turn), int(result["round"]), False
            task = conn.execute(
                "SELECT t.*, g.status AS group_status FROM collaboration_tasks t "
                "JOIN collaboration_groups g ON g.group_id=t.group_id "
                "WHERE t.task_id=? AND g.owner_key=?",
                (task_id, self.owner_key),
            ).fetchone()
            if task is None:
                raise RuntimeError("collaboration task is unavailable")
            if task["group_status"] != "active":
                raise RuntimeError("collaboration group is archived")
            if task["status"] != "open":
                raise RuntimeError("collaboration task is not open")
            if int(task["round"]) >= int(task["max_rounds"]):
                raise RuntimeError("collaboration task round limit reached")
            active = conn.execute(
                "SELECT 1 FROM collaboration_turns WHERE group_id=? "
                "AND status IN ('queued','running') LIMIT 1",
                (task["group_id"],),
            ).fetchone()
            if active is not None:
                raise RuntimeError("collaboration round is still running")
            memberships = {
                str(row["employee_id"]): row
                for row in conn.execute(
                    "SELECT * FROM collaboration_memberships WHERE group_id=? "
                    "AND leave_sequence IS NULL",
                    (task["group_id"],),
                ).fetchall()
            }
            if not set(targets) <= set(memberships):
                raise RuntimeError("collaboration target is not an active member")
            allowed = set(json.loads(str(task["allowed_attachment_ids_json"] or "[]")))
            if not set(attachments) <= allowed:
                raise RuntimeError("collaboration attachment is not allowed for this task")
            event_row = self._append_event(
                conn,
                group_id=str(task["group_id"]),
                event_kind="message.owner",
                actor_kind="system",
                body={
                    "text": instruction,
                    "mentions": [memberships[value]["membership_id"] for value in targets],
                    "mention_all": False,
                    "ai_round": int(task["round"]) + 1,
                },
                now=now,
            )
            event = self._event(event_row)
            turn_id = f"ct_{uuid.uuid4().hex}"
            conn.execute(
                "INSERT INTO collaboration_turns "
                "(turn_id, group_id, event_id, snapshot_sequence, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'queued', ?, ?)",
                (turn_id, task["group_id"], event.event_id, event.sequence, now, now),
            )
            result_targets = []
            for employee_id in targets:
                membership = memberships[employee_id]
                target_id = f"ctt_{uuid.uuid4().hex}"
                execution_id = f"cex_{uuid.uuid4().hex}"
                conn.execute(
                    "INSERT INTO collaboration_turn_targets "
                    "(target_id, execution_id, turn_id, employee_id, membership_id, "
                    "session_generation, join_sequence, snapshot_sequence, status, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                    (
                        target_id,
                        execution_id,
                        turn_id,
                        employee_id,
                        membership["membership_id"],
                        membership["current_session_generation"],
                        membership["join_sequence"],
                        event.sequence,
                        now,
                        now,
                    ),
                )
                for attachment_id in attachments:
                    conn.execute(
                        "INSERT INTO collaboration_attachment_grants "
                        "(grant_id, attachment_id, target_id, granted_sequence, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            f"cag_{uuid.uuid4().hex}",
                            attachment_id,
                            target_id,
                            event.sequence,
                            now,
                        ),
                    )
                result_targets.append(
                    CollaborationTarget(
                        target_id=target_id,
                        execution_id=execution_id,
                        turn_id=turn_id,
                        employee_id=employee_id,
                        membership_id=str(membership["membership_id"]),
                        session_generation=int(membership["current_session_generation"]),
                        join_sequence=int(membership["join_sequence"]),
                        snapshot_sequence=event.sequence,
                        status="queued",
                        error=None,
                        result=None,
                        last_delivered_sequence=0,
                        active_seconds=0,
                        active_started_at=None,
                        attempt=0,
                    )
                )
            next_round = int(task["round"]) + 1
            conn.execute(
                "UPDATE collaboration_tasks SET round=?, updated_at=? WHERE task_id=?",
                (next_round, now, task_id),
            )
            conn.execute(
                "UPDATE collaboration_origins SET round=? WHERE group_id=?",
                (next_round, task["group_id"]),
            )
            result = {
                "task_id": task_id,
                "group_id": str(task["group_id"]),
                "event_id": event.event_id,
                "turn_id": turn_id,
                "round": next_round,
                "status": "queued",
            }
            conn.execute(
                "INSERT INTO collaboration_agent_receipts "
                "(receipt_id, owner_key, operation, idempotency_key, request_json, "
                "result_json, created_at) VALUES (?, ?, 'dispatch', ?, ?, ?, ?)",
                (
                    f"car_{uuid.uuid4().hex}",
                    self.owner_key,
                    idempotency_key,
                    request_json,
                    json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    now,
                ),
            )
            return (
                SubmittedOwnerMessage(
                    event=event,
                    turn=CollaborationTurn(
                        turn_id=turn_id,
                        group_id=str(task["group_id"]),
                        event_id=event.event_id,
                        snapshot_sequence=event.sequence,
                        status="queued",
                        targets=tuple(result_targets),
                    ),
                ),
                next_round,
                True,
            )

        return self.db._execute_write(_write)

    def active_memberships(self, group_id: str) -> tuple[CollaborationMembership, ...]:
        group = self.get_group(group_id)
        with self.db._lock:
            rows = self.db._conn.execute(
                "SELECT * FROM collaboration_memberships "
                "WHERE group_id=? AND leave_sequence IS NULL "
                "ORDER BY join_sequence, membership_id",
                (group.group_id,),
            ).fetchall()
        return tuple(self._membership(dict(row)) for row in rows)

    def snapshot_payload(
        self,
        group_id: str,
        *,
        limit: int = DEFAULT_HISTORY_PAGE_SIZE,
        before_sequence: int | None = None,
        after_sequence: int | None = None,
        through_sequence: int | None = None,
        reconcile_membership_ids: Iterable[str] = (),
        reconcile_target_ids: Iterable[str] = (),
        reconcile_approval_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        group_id = _identifier(group_id, "group ID")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_HISTORY_PAGE_SIZE
        ):
            raise ValueError(
                f"collaboration history limit must be between 1 and {MAX_HISTORY_PAGE_SIZE}"
            )
        if before_sequence is not None and after_sequence is not None:
            raise ValueError("before_sequence and after_sequence are mutually exclusive")
        if through_sequence is not None and after_sequence is None:
            raise ValueError("through_sequence requires after_sequence")
        requested_before = _optional_sequence(before_sequence, "before_sequence", minimum=1)
        requested_after = _optional_sequence(after_sequence, "after_sequence", minimum=0)
        requested_through = _optional_sequence(through_sequence, "through_sequence", minimum=0)
        membership_ids = _reconciliation_ids(reconcile_membership_ids, "membership")
        target_ids = _reconciliation_ids(reconcile_target_ids, "target")
        approval_ids = _reconciliation_ids(reconcile_approval_ids, "approval")

        with self.db._lock:
            conn = self.db._conn
            group_row = self._owned_group_row(conn, group_id)
            if group_row is None:
                raise RuntimeError("collaboration group is unavailable")
            group = self._group(dict(group_row))
            high_water = group.last_sequence if requested_through is None else requested_through
            if high_water > group.last_sequence:
                raise ValueError("through_sequence exceeds the group high-water mark")
            if requested_after is not None and high_water < requested_after:
                raise ValueError("through_sequence must not precede after_sequence")

            if requested_after is not None:
                direction = "forward"
                rows = conn.execute(
                    "SELECT * FROM collaboration_events WHERE group_id=? "
                    "AND sequence>? AND sequence<=? ORDER BY sequence LIMIT ?",
                    (group_id, requested_after, high_water, limit + 1),
                ).fetchall()
                has_more = len(rows) > limit
                event_rows = rows[:limit]
            else:
                direction = "backward" if requested_before is not None else "initial"
                upper_bound = min(requested_before or high_water + 1, high_water + 1)
                rows = conn.execute(
                    "SELECT * FROM collaboration_events WHERE group_id=? "
                    "AND sequence<? ORDER BY sequence DESC LIMIT ?",
                    (group_id, upper_bound, limit + 1),
                ).fetchall()
                has_more = len(rows) > limit
                event_rows = list(reversed(rows[:limit]))

            events = [self._event(dict(row)).__dict__ for row in event_rows]
            event_ids = tuple(str(item["event_id"]) for item in events)
            effective_target_ids = set(target_ids)
            if approval_ids:
                approval_target_rows = conn.execute(
                    "SELECT a.target_id FROM collaboration_approvals a "
                    "JOIN collaboration_turn_targets tt ON tt.target_id=a.target_id "
                    "JOIN collaboration_turns t ON t.turn_id=tt.turn_id "
                    f"WHERE t.group_id=? AND a.approval_id IN ({_sql_placeholders(approval_ids)})",
                    (group_id, *approval_ids),
                ).fetchall()
                effective_target_ids.update(str(row["target_id"]) for row in approval_target_rows)
            ordered_target_ids = tuple(sorted(effective_target_ids))
            turn_terms = ["t.status NOT IN ('completed','partial','failed','ambiguous','cancelled')"]
            turn_args: list[Any] = [group_id]
            if event_ids:
                turn_terms.append(f"t.event_id IN ({_sql_placeholders(event_ids)})")
                turn_args.extend(event_ids)
            if ordered_target_ids:
                turn_terms.append(
                    "EXISTS (SELECT 1 FROM collaboration_turn_targets rt "
                    f"WHERE rt.turn_id=t.turn_id AND rt.target_id IN ({_sql_placeholders(ordered_target_ids)}))"
                )
                turn_args.extend(ordered_target_ids)
            turns = conn.execute(
                "SELECT t.* FROM collaboration_turns t WHERE t.group_id=? AND ("
                + " OR ".join(turn_terms)
                + ") ORDER BY t.created_at, t.turn_id",
                tuple(turn_args),
            ).fetchall()
            turn_ids = tuple(str(row["turn_id"]) for row in turns)

            target_terms = ["tt.status IN ('queued','running','waiting_approval')"]
            target_args: list[Any] = [group_id]
            if turn_ids:
                target_terms.append(f"tt.turn_id IN ({_sql_placeholders(turn_ids)})")
                target_args.extend(turn_ids)
            if ordered_target_ids:
                target_terms.append(f"tt.target_id IN ({_sql_placeholders(ordered_target_ids)})")
                target_args.extend(ordered_target_ids)
            targets = conn.execute(
                "SELECT tt.* FROM collaboration_turn_targets tt "
                "JOIN collaboration_turns t ON t.turn_id=tt.turn_id WHERE t.group_id=? AND ("
                + " OR ".join(target_terms)
                + ") ORDER BY t.created_at, tt.employee_id, tt.target_id",
                tuple(target_args),
            ).fetchall()
            included_target_ids = tuple(str(row["target_id"]) for row in targets)

            approval_terms = ["a.status='pending'"]
            approval_args: list[Any] = [group_id]
            if included_target_ids:
                approval_terms.append(f"a.target_id IN ({_sql_placeholders(included_target_ids)})")
                approval_args.extend(included_target_ids)
            if approval_ids:
                approval_terms.append(f"a.approval_id IN ({_sql_placeholders(approval_ids)})")
                approval_args.extend(approval_ids)
            approvals = conn.execute(
                "SELECT a.*, tt.execution_id, tt.turn_id, t.group_id "
                "FROM collaboration_approvals a "
                "JOIN collaboration_turn_targets tt ON tt.target_id=a.target_id "
                "JOIN collaboration_turns t ON t.turn_id=tt.turn_id WHERE t.group_id=? AND ("
                + " OR ".join(approval_terms)
                + ") ORDER BY a.created_at, a.approval_id",
                tuple(approval_args),
            ).fetchall()

            referenced_memberships = set(membership_ids)
            referenced_memberships.update(
                str(event["actor_membership_id"])
                for event in events
                if event.get("actor_membership_id")
            )
            for event in events:
                mentions = event.get("body", {}).get("mentions", [])
                if isinstance(mentions, list):
                    referenced_memberships.update(str(value) for value in mentions if value)
            referenced_memberships.update(str(row["membership_id"]) for row in targets)
            membership_terms = ["leave_sequence IS NULL"]
            membership_args: list[Any] = [group_id]
            if referenced_memberships:
                ordered_memberships = tuple(sorted(referenced_memberships))
                membership_terms.append(
                    f"membership_id IN ({_sql_placeholders(ordered_memberships)})"
                )
                membership_args.extend(ordered_memberships)
            memberships = conn.execute(
                "SELECT * FROM collaboration_memberships WHERE group_id=? AND ("
                + " OR ".join(membership_terms)
                + ") ORDER BY join_sequence, membership_id",
                tuple(membership_args),
            ).fetchall()

            attachments = []
            if event_ids:
                attachments = conn.execute(
                    "SELECT attachment_id, group_id, event_id, filename, media_type, "
                    "size_bytes, content_sha256, created_at FROM collaboration_attachments "
                    f"WHERE group_id=? AND event_id IN ({_sql_placeholders(event_ids)}) "
                    "ORDER BY created_at, attachment_id",
                    (group_id, *event_ids),
                ).fetchall()

        range_start = int(event_rows[0]["sequence"]) if event_rows else None
        range_end = int(event_rows[-1]["sequence"]) if event_rows else None
        next_before = range_start if has_more and direction != "forward" else None
        next_after = range_end if direction == "forward" and event_rows else requested_after
        return {
            "group": group.__dict__,
            "memberships": [self._membership(dict(row)).__dict__ for row in memberships],
            "events": events,
            "turns": [dict(row) for row in turns],
            "targets": [dict(row) for row in targets],
            "approvals": [dict(row) for row in approvals],
            "attachments": [dict(row) for row in attachments],
            "history_page": {
                "direction": direction,
                "limit": limit,
                "snapshot_sequence": high_water,
                "range_start_sequence": range_start,
                "range_end_sequence": range_end,
                "before_sequence": requested_before,
                "next_before_sequence": next_before,
                "after_sequence": requested_after,
                "next_after_sequence": next_after,
                "through_sequence": high_water,
                "has_more": has_more,
            },
            "reconciliation": {
                "after_sequence": requested_after or 0,
                "last_sequence": high_water,
                "next_after_sequence": next_after if direction == "forward" else high_water,
                "snapshot_authoritative": True,
            },
        }

    def list_events_payload(
        self,
        group_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        group = self.get_group(group_id)
        after_sequence = max(0, int(after_sequence))
        limit = max(1, min(int(limit), 500))
        with self.db._lock:
            rows = self.db._conn.execute(
                "SELECT * FROM collaboration_events WHERE group_id=? AND sequence>? "
                "ORDER BY sequence LIMIT ?",
                (group.group_id, after_sequence, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        page = rows[:limit]
        return {
            "group_id": group.group_id,
            "last_sequence": group.last_sequence,
            "events": [self._event(dict(row)).__dict__ for row in page],
            "has_more": has_more,
            "next_after_sequence": (
                int(page[-1]["sequence"]) if page else after_sequence
            ),
        }

    def _turn(self, conn, row: Mapping[str, Any]) -> CollaborationTurn:
        targets = conn.execute(
            "SELECT * FROM collaboration_turn_targets WHERE turn_id=? "
            "ORDER BY created_at, employee_id",
            (row["turn_id"],),
        ).fetchall()
        return CollaborationTurn(
            turn_id=str(row["turn_id"]),
            group_id=str(row["group_id"]),
            event_id=str(row["event_id"]),
            snapshot_sequence=int(row["snapshot_sequence"]),
            status=str(row["status"]),
            targets=tuple(self._target(dict(target)) for target in targets),
        )

    @staticmethod
    def _target(row: Mapping[str, Any]) -> CollaborationTarget:
        result = json.loads(str(row["result_json"])) if row["result_json"] else None
        return CollaborationTarget(
            target_id=str(row["target_id"]),
            execution_id=str(row["execution_id"]),
            turn_id=str(row["turn_id"]),
            employee_id=str(row["employee_id"]),
            membership_id=str(row["membership_id"]),
            session_generation=int(row["session_generation"]),
            join_sequence=int(row["join_sequence"]),
            snapshot_sequence=int(row["snapshot_sequence"]),
            status=str(row["status"]),
            error=str(row["error"]) if row["error"] is not None else None,
            result=result,
            last_delivered_sequence=int(row["last_delivered_sequence"]),
            active_seconds=float(row["active_seconds"]),
            active_started_at=(
                float(row["active_started_at"])
                if row["active_started_at"] is not None
                else None
            ),
            attempt=int(row["attempt"]),
        )

    def _owned_group_row(self, conn, group_id: str):
        return conn.execute(
            "SELECT * FROM collaboration_groups WHERE group_id=? AND owner_key=?",
            (group_id, self.owner_key),
        ).fetchone()

    def _require_active_group(self, conn, group_id: str):
        row = self._owned_group_row(conn, group_id)
        if row is None or row["status"] != "active":
            raise RuntimeError("active collaboration group is unavailable")
        return row

    def _append_event(
        self,
        conn,
        *,
        group_id: str,
        event_kind: str,
        actor_kind: str,
        body: Mapping[str, Any],
        now: float,
        actor_employee_id: str | None = None,
        actor_membership_id: str | None = None,
    ) -> dict[str, Any]:
        group = self._owned_group_row(conn, group_id)
        if group is None:
            raise RuntimeError("collaboration group is unavailable")
        if actor_kind == "employee":
            actor_employee_id = _identifier(actor_employee_id, "actor employee ID")
            actor_membership_id = _identifier(
                actor_membership_id, "actor membership ID"
            )
            actor = conn.execute(
                "SELECT employee_id, group_id FROM collaboration_memberships "
                "WHERE membership_id=?",
                (actor_membership_id,),
            ).fetchone()
            if (
                actor is None
                or actor["group_id"] != group_id
                or actor["employee_id"] != actor_employee_id
            ):
                raise RuntimeError("employee event actor membership is inconsistent")
        elif actor_kind in {"owner", "system"}:
            if actor_membership_id is not None:
                raise ValueError("non-employee events cannot have an actor membership")
        else:
            raise ValueError("event actor kind is invalid")
        sequence = int(group["last_sequence"]) + 1
        event_id = f"ce_{uuid.uuid4().hex}"
        conn.execute(
            """
            INSERT INTO collaboration_events
              (event_id, group_id, sequence, event_kind, actor_kind,
               actor_employee_id, actor_membership_id, body_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                group_id,
                sequence,
                event_kind,
                actor_kind,
                actor_employee_id,
                actor_membership_id,
                _json_object(body),
                now,
            ),
        )
        conn.execute(
            "UPDATE collaboration_groups SET last_sequence=?, updated_at=? WHERE group_id=?",
            (sequence, now, group_id),
        )
        return dict(
            conn.execute(
                "SELECT * FROM collaboration_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        )

    def _add_membership(
        self,
        conn,
        *,
        group_id: str,
        member: CollaborationMemberProfile,
        actor_kind: str,
        now: float,
        role: str = "member",
    ) -> dict[str, Any]:
        employee_id = member.employee_id
        if role not in {"member", "owner"}:
            raise ValueError("collaboration membership role is invalid")
        if conn.execute(
            "SELECT 1 FROM collaboration_memberships "
            "WHERE group_id=? AND employee_id=? AND leave_sequence IS NULL",
            (group_id, employee_id),
        ).fetchone() is not None:
            raise RuntimeError("collaboration member is already active")
        membership_id = f"cm_{uuid.uuid4().hex}"
        hidden_session_id = f"collab_member_{uuid.uuid4().hex}"
        stored_session_id = f"collab_stored_{uuid.uuid4().hex}"
        event = self._append_event(
            conn,
            group_id=group_id,
            event_kind="membership.joined",
            actor_kind=actor_kind,
            body={"employee_id": employee_id, "membership_id": membership_id},
            now=now,
        )
        conn.execute(
            """
            INSERT INTO collaboration_memberships
              (membership_id, group_id, employee_id, profile_revision,
               profile_fingerprint, hidden_session_id, stored_session_id,
               role, join_sequence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                membership_id,
                group_id,
                employee_id,
                member.profile_revision,
                member.profile_fingerprint,
                hidden_session_id,
                stored_session_id,
                role,
                event["sequence"],
                now,
            ),
        )
        return dict(
            conn.execute(
                "SELECT * FROM collaboration_memberships WHERE membership_id=?",
                (membership_id,),
            ).fetchone()
        )

    def record_initial_member_session_generation(
        self,
        conn,
        *,
        membership: CollaborationMembership,
        employee_policy: Mapping[str, Any],
    ) -> None:
        """Persist immutable generation 1 beside its already-provisioned session."""
        policy = dict(employee_policy)
        fingerprint = _identifier(
            policy.get("snapshot_fingerprint"), "employee policy fingerprint"
        )
        row = conn.execute(
            "SELECT event_id FROM collaboration_events WHERE group_id=? AND sequence=?",
            (membership.group_id, membership.join_sequence),
        ).fetchone()
        if row is None:
            raise RuntimeError("collaboration membership event is unavailable")
        conn.execute(
            "INSERT INTO collaboration_member_session_generations "
            "(membership_id, generation, profile_revision, profile_fingerprint, "
            "policy_fingerprint, policy_json, hidden_session_id, stored_session_id, "
            "activated_event_id, activated_sequence, created_at) "
            "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                membership.membership_id,
                membership.profile_revision,
                membership.profile_fingerprint,
                fingerprint,
                _json_object(policy),
                membership.hidden_session_id,
                membership.stored_session_id,
                row["event_id"],
                membership.join_sequence,
                membership.created_at,
            ),
        )

    def ensure_current_member_session_generation(
        self,
        conn,
        *,
        membership_id: str,
        member: CollaborationMemberProfile,
        employee_policy: Mapping[str, Any],
        provision_session,
        now: float,
    ) -> tuple[CollaborationMemberSessionGeneration, CollaborationEvent | None]:
        """Rotate future execution to one fresh immutable session when policy changes."""
        membership = conn.execute(
            "SELECT * FROM collaboration_memberships WHERE membership_id=?",
            (membership_id,),
        ).fetchone()
        if membership is None or membership["leave_sequence"] is not None:
            raise RuntimeError("collaboration membership is no longer eligible")
        policy = dict(employee_policy)
        fingerprint = _identifier(
            policy.get("snapshot_fingerprint"), "employee policy fingerprint"
        )
        policy_json = _json_object(policy)
        current = conn.execute(
            "SELECT * FROM collaboration_member_session_generations "
            "WHERE membership_id=? AND generation=?",
            (membership_id, membership["current_session_generation"]),
        ).fetchone()
        if (
            current is not None
            and str(current["policy_fingerprint"]) == fingerprint
            and str(current["policy_json"]) == policy_json
        ):
            return self._session_generation(dict(current)), None

        generation = int(membership["current_session_generation"]) + 1
        hidden_session_id = f"collab_member_{uuid.uuid4().hex}"
        stored_session_id = f"collab_stored_{uuid.uuid4().hex}"
        event = self._event(
            self._append_event(
                conn,
                group_id=str(membership["group_id"]),
                event_kind="membership.session.rotated",
                actor_kind="system",
                body={
                    "membership_id": membership_id,
                    "employee_id": member.employee_id,
                    "reason": (
                        "effective_policy_changed"
                        if current is not None
                        else "legacy_generation_unavailable"
                    ),
                    "from_generation": int(membership["current_session_generation"]),
                    "to_generation": generation,
                    "from_profile_revision": (
                        int(current["profile_revision"]) if current is not None else None
                    ),
                    "to_profile_revision": member.profile_revision,
                    "from_profile_fingerprint": (
                        str(current["profile_fingerprint"]) if current is not None else None
                    ),
                    "to_profile_fingerprint": member.profile_fingerprint,
                    "from_policy_fingerprint": (
                        str(current["policy_fingerprint"]) if current is not None else None
                    ),
                    "to_policy_fingerprint": fingerprint,
                },
                now=now,
            )
        )
        generation_row = CollaborationMemberSessionGeneration(
            membership_id=membership_id,
            generation=generation,
            profile_revision=member.profile_revision,
            profile_fingerprint=member.profile_fingerprint,
            policy_fingerprint=fingerprint,
            employee_policy=policy,
            hidden_session_id=hidden_session_id,
            stored_session_id=stored_session_id,
            activated_event_id=event.event_id,
            activated_sequence=event.sequence,
            created_at=now,
        )
        provision_session(generation_row, policy)
        conn.execute(
            "INSERT INTO collaboration_member_session_generations "
            "(membership_id, generation, profile_revision, profile_fingerprint, "
            "policy_fingerprint, policy_json, hidden_session_id, stored_session_id, "
            "activated_event_id, activated_sequence, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                membership_id,
                generation,
                member.profile_revision,
                member.profile_fingerprint,
                fingerprint,
                policy_json,
                hidden_session_id,
                stored_session_id,
                event.event_id,
                event.sequence,
                now,
            ),
        )
        changed = conn.execute(
            "UPDATE collaboration_memberships SET current_session_generation=? "
            "WHERE membership_id=? AND current_session_generation=?",
            (generation, membership_id, membership["current_session_generation"]),
        ).rowcount
        if changed != 1:
            raise RuntimeError("collaboration session generation changed concurrently")
        conn.execute(
            "UPDATE collaboration_turn_targets SET session_generation=?, updated_at=? "
            "WHERE membership_id=? AND status='queued'",
            (generation, now, membership_id),
        )
        return generation_row, event

    @staticmethod
    def _trusted_profile(member: CollaborationMemberProfile) -> CollaborationMemberProfile:
        if not isinstance(member, CollaborationMemberProfile):
            raise TypeError("trusted collaboration member profile is required")
        employee_id = _identifier(member.employee_id, "employee ID")
        if (
            isinstance(member.profile_revision, bool)
            or not isinstance(member.profile_revision, int)
            or member.profile_revision < 1
        ):
            raise ValueError("profile revision must be a positive integer")
        profile_fingerprint = _identifier(
            member.profile_fingerprint, "profile fingerprint"
        )
        return CollaborationMemberProfile(
            employee_id=employee_id,
            profile_revision=int(member.profile_revision),
            profile_fingerprint=profile_fingerprint,
        )

    @staticmethod
    def _group(row: Mapping[str, Any]) -> CollaborationGroup:
        return CollaborationGroup(
            group_id=str(row["group_id"]),
            owner_key=str(row["owner_key"]),
            name=str(row["name"]),
            creator_kind=str(row["creator_kind"]),
            creator_employee_id=(
                str(row["creator_employee_id"])
                if row["creator_employee_id"] is not None
                else None
            ),
            status=str(row["status"]),
            last_sequence=int(row["last_sequence"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            archived_at=float(row["archived_at"]) if row["archived_at"] is not None else None,
        )

    @staticmethod
    def _membership(row: Mapping[str, Any]) -> CollaborationMembership:
        return CollaborationMembership(
            membership_id=str(row["membership_id"]),
            group_id=str(row["group_id"]),
            employee_id=str(row["employee_id"]),
            profile_revision=int(row["profile_revision"]),
            profile_fingerprint=str(row["profile_fingerprint"]),
            hidden_session_id=str(row["hidden_session_id"]),
            stored_session_id=str(row["stored_session_id"]),
            current_session_generation=int(row["current_session_generation"]),
            role=str(row["role"]),
            join_sequence=int(row["join_sequence"]),
            leave_sequence=int(row["leave_sequence"]) if row["leave_sequence"] is not None else None,
            created_at=float(row["created_at"]),
            left_at=float(row["left_at"]) if row["left_at"] is not None else None,
        )

    @staticmethod
    def _session_generation(
        row: Mapping[str, Any],
    ) -> CollaborationMemberSessionGeneration:
        return CollaborationMemberSessionGeneration(
            membership_id=str(row["membership_id"]),
            generation=int(row["generation"]),
            profile_revision=int(row["profile_revision"]),
            profile_fingerprint=str(row["profile_fingerprint"]),
            policy_fingerprint=str(row["policy_fingerprint"]),
            employee_policy=json.loads(str(row["policy_json"])),
            hidden_session_id=str(row["hidden_session_id"]),
            stored_session_id=str(row["stored_session_id"]),
            activated_event_id=str(row["activated_event_id"]),
            activated_sequence=int(row["activated_sequence"]),
            created_at=float(row["created_at"]),
        )

    @staticmethod
    def _event(row: Mapping[str, Any]) -> CollaborationEvent:
        body = json.loads(str(row["body_json"]))
        return CollaborationEvent(
            event_id=str(row["event_id"]),
            group_id=str(row["group_id"]),
            sequence=int(row["sequence"]),
            event_kind=str(row["event_kind"]),
            actor_kind=str(row["actor_kind"]),
            actor_employee_id=(
                str(row["actor_employee_id"])
                if row["actor_employee_id"] is not None
                else None
            ),
            actor_membership_id=(
                str(row["actor_membership_id"])
                if row["actor_membership_id"] is not None
                else None
            ),
            body=body,
            created_at=float(row["created_at"]),
        )
