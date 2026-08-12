from __future__ import annotations

import base64
import sqlite3

import pytest

from hermes_cli.collaboration import CollaborationMemberProfile, CollaborationStore
from hermes_state import SessionDB


def _member(employee_id: str, *, revision: int = 1) -> CollaborationMemberProfile:
    return CollaborationMemberProfile(
        employee_id=employee_id,
        profile_revision=revision,
        profile_fingerprint=f"fingerprint-{employee_id}-r{revision}",
    )


@pytest.fixture
def db(tmp_path):
    store = SessionDB(tmp_path / "state.db")
    try:
        yield store
    finally:
        store.close()


def test_group_membership_sequences_and_owner_scope(db):
    first = CollaborationStore(db, owner_key="owner-a")
    second = CollaborationStore(db, owner_key="owner-b")

    group = first.create_group("Delivery", members=[_member("employee-a")])
    assert group.creator_kind == "owner"
    assert group.creator_employee_id is None
    assert group.last_sequence == 2
    pinned = first.snapshot_payload(group.group_id)["memberships"][0]
    assert pinned["profile_revision"] == 1
    assert pinned["profile_fingerprint"] == "fingerprint-employee-a-r1"
    assert pinned["hidden_session_id"].startswith("collab_member_")
    assert pinned["stored_session_id"].startswith("collab_stored_")
    with pytest.raises(TypeError, match="trusted"):
        first.add_membership(group.group_id, "employee-b")
    assert [item.group_id for item in first.list_groups()] == [group.group_id]
    assert second.list_groups() == ()
    with pytest.raises(RuntimeError, match="unavailable"):
        second.get_group(group.group_id)

    left = first.remove_membership(group.group_id, "employee-a")
    assert left.join_sequence == 2
    assert left.leave_sequence == 3
    rejoined = first.add_membership(group.group_id, _member("employee-a", revision=2))
    assert rejoined.join_sequence == 4
    assert rejoined.membership_id != left.membership_id
    assert rejoined.profile_revision == 2
    assert rejoined.profile_fingerprint == "fingerprint-employee-a-r2"
    assert rejoined.hidden_session_id != left.hidden_session_id
    assert rejoined.stored_session_id != left.stored_session_id
    with pytest.raises(RuntimeError, match="already active"):
        first.add_membership(group.group_id, _member("employee-a", revision=3))

    with db._lock:
        active = db._conn.execute(
            "SELECT COUNT(*) FROM collaboration_memberships "
            "WHERE group_id=? AND employee_id=? AND leave_sequence IS NULL",
            (group.group_id, "employee-a"),
        ).fetchone()[0]
    assert active == 1

    archived, live_sessions = first.archive_group(group.group_id)
    assert archived.status == "archived"
    assert live_sessions == ()
    assert first.list_groups() == ()
    assert first.list_groups(include_archived=True)[0].group_id == group.group_id
    for operation in (
        lambda: first.add_membership(group.group_id, _member("employee-b")),
        lambda: first.create_attachment(
            group.group_id,
            filename="note.txt",
            media_type="text/plain",
            size_bytes=4,
            storage_key="groups/note.txt",
            content_sha256="a" * 64,
        ),
        lambda: first.submit_owner_message(group.group_id, text="late message"),
    ):
        with pytest.raises(RuntimeError, match="active collaboration group"):
            operation()


def test_group_create_idempotency_replays_exact_request_and_rolls_back_provisioning(db):
    store = CollaborationStore(db, owner_key="owner-a")
    request = {
        "name": "Idempotent",
        "members": [_member("employee-a")],
        "client_idempotency_key": "create-key",
    }

    first = store.create_group(**request)
    replay = store.create_group(**request)
    assert replay.group_id == first.group_id
    with pytest.raises(RuntimeError, match="idempotency key request mismatch"):
        store.create_group(
            "Different",
            members=request["members"],
            client_idempotency_key="create-key",
        )

    def fail_provision(_membership, _member):
        raise RuntimeError("provision failed")

    with pytest.raises(RuntimeError, match="provision failed"):
        store.create_group(
            "Rollback",
            members=[_member("employee-b")],
            client_idempotency_key="rollback-key",
            provision_member=fail_provision,
        )
    with db._lock:
        assert db._conn.execute(
            "SELECT COUNT(*) FROM collaboration_groups WHERE name='Rollback'"
        ).fetchone()[0] == 0
        assert db._conn.execute(
            "SELECT COUNT(*) FROM collaboration_group_receipts "
            "WHERE idempotency_key='rollback-key'"
        ).fetchone()[0] == 0


def test_membership_update_rolls_back_complete_delta_when_provisioning_fails(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group("Atomic", members=[_member("employee-a")])
    before = store.snapshot_payload(group.group_id)

    def fail_provision(_membership, _member):
        raise RuntimeError("provision failed")

    with pytest.raises(RuntimeError, match="provision failed"):
        store.update_memberships(
            group.group_id,
            requested_employee_ids=["employee-b"],
            additions={"employee-b": _member("employee-b")},
            provision_member=fail_provision,
        )

    after = store.snapshot_payload(group.group_id)
    assert after["memberships"] == before["memberships"]
    assert after["events"] == before["events"]


def test_employee_creator_requires_employee_identity(db):
    store = CollaborationStore(db, owner_key="owner-a")
    with pytest.raises(ValueError, match="creator employee ID"):
        store.create_group("Employee-created", creator_kind="employee")
    group = store.create_group(
        "Employee-created",
        creator_kind="employee",
        creator_employee_id="employee-a",
    )
    assert group.creator_kind == "employee"
    assert group.creator_employee_id == "employee-a"


def test_collaboration_upload_validation_covers_image_file_pdf_and_limits(monkeypatch):
    from hermes_cli import attachment_uploads

    png = b"\x89PNG\r\n\x1a\ncontent"
    image = attachment_uploads.validate_upload(
        kind="image",
        filename="photo.bin",
        content_base64=base64.b64encode(png).decode("ascii"),
        media_type="image/png",
    )
    assert image.filename == "photo.png"
    assert image.media_type == "image/png"

    text = attachment_uploads.validate_upload(
        kind="file",
        filename="note.txt",
        content_base64=base64.b64encode(b"hello").decode("ascii"),
        media_type="text/plain",
    )
    assert text.data == b"hello"
    with pytest.raises(ValueError, match="executable"):
        attachment_uploads.validate_upload(
            kind="file",
            filename="unsafe.exe",
            content_base64=base64.b64encode(b"MZ").decode("ascii"),
        )

    pdf = b"%PDF-1.7\n/Type /Page\n%%EOF"
    validated_pdf = attachment_uploads.validate_upload(
        kind="pdf",
        filename="report.pdf",
        content_base64=base64.b64encode(pdf).decode("ascii"),
        media_type="application/pdf",
    )
    assert validated_pdf.media_type == "application/pdf"
    with pytest.raises(ValueError, match="valid PDF"):
        attachment_uploads.validate_upload(
            kind="pdf",
            filename="bad.pdf",
            content_base64=base64.b64encode(b"not-pdf").decode("ascii"),
        )
    too_many_pages = b"%PDF-1.7\n" + b"/Type /Page\n" * 26 + b"%%EOF"
    with pytest.raises(ValueError, match="page limit"):
        attachment_uploads.validate_upload(
            kind="pdf",
            filename="long.pdf",
            content_base64=base64.b64encode(too_many_pages).decode("ascii"),
        )

    monkeypatch.setattr(attachment_uploads, "FILE_MAX_BYTES", 3)
    with pytest.raises(ValueError, match="25 MB"):
        attachment_uploads.validate_upload(
            kind="file",
            filename="large.txt",
            content_base64=base64.b64encode(b"four").decode("ascii"),
            media_type="text/plain",
        )


def test_owner_message_without_mentions_targets_first_available_employee(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Default reply", members=[_member("employee-a"), _member("employee-b")]
    )

    submitted = store.submit_owner_message(group.group_id, text="Who can help?")

    assert submitted.turn is not None
    assert [target.employee_id for target in submitted.turn.targets] == ["employee-a"]
    assert submitted.event.body["mentions"] == [submitted.turn.targets[0].membership_id]


def test_owner_message_without_mentions_keeps_explicit_employee_current(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Continue explicit", members=[_member("employee-a"), _member("employee-b")]
    )
    membership_b = next(
        membership
        for membership in store.active_memberships(group.group_id)
        if membership.employee_id == "employee-b"
    )
    store.submit_owner_message(
        group.group_id,
        text="Please start",
        mentioned_membership_ids=[membership_b.membership_id],
    )

    submitted = store.submit_owner_message(group.group_id, text="Please continue")

    assert submitted.turn is not None
    assert [target.employee_id for target in submitted.turn.targets] == ["employee-b"]


def test_owner_message_without_mentions_targets_latest_replying_employee(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Continue reply", members=[_member("employee-a"), _member("employee-b")]
    )
    membership_b = next(
        membership
        for membership in store.active_memberships(group.group_id)
        if membership.employee_id == "employee-b"
    )
    with db._lock:
        store._append_event(
            db._conn,
            group_id=group.group_id,
            event_kind="message.employee",
            actor_kind="employee",
            actor_employee_id="employee-b",
            actor_membership_id=membership_b.membership_id,
            body={"text": "I can help"},
            now=123.0,
        )

    submitted = store.submit_owner_message(group.group_id, text="Please continue")

    assert submitted.turn is not None
    assert [target.employee_id for target in submitted.turn.targets] == ["employee-b"]


def test_owner_message_without_available_employees_remains_background_context(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group("Quiet")

    submitted = store.submit_owner_message(group.group_id, text="For awareness")

    assert submitted.turn is None
    assert submitted.event.body["mentions"] == []


def test_mentioned_turn_snapshots_members_and_attachment_grants_atomically(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group(
        "Review", members=[_member("employee-a"), _member("employee-b")]
    )

    membership_b = next(
        member
        for member in store.active_memberships(group.group_id)
        if member.employee_id == "employee-b"
    )
    submitted = store.submit_owner_message(
        group.group_id,
        text="Review the artifact",
        mentioned_membership_ids=[membership_b.membership_id],
        attachment_ids=[store.create_attachment(
            group.group_id, filename="report.txt", media_type="text/plain",
            size_bytes=12, storage_key="groups/review/report", content_sha256="b" * 64,
        )["attachment_id"]],
    )

    assert submitted.turn is not None
    assert submitted.turn.status == "queued"
    assert submitted.turn.snapshot_sequence == submitted.event.sequence
    assert [target.employee_id for target in submitted.turn.targets] == ["employee-b"]
    target = submitted.turn.targets[0]
    assert target.execution_id.startswith("cex_")
    assert target.status == "queued"
    assert target.snapshot_sequence == submitted.event.sequence
    assert target.last_delivered_sequence == 0
    assert target.active_seconds == 0
    assert target.active_started_at is None
    assert target.attempt == 0
    with db._lock:
        assert db._conn.execute(
            "SELECT COUNT(*) FROM collaboration_attachment_grants WHERE target_id=?",
            (target.target_id,),
        ).fetchone()[0] == 1

    all_submitted = store.submit_owner_message(
        group.group_id, text="Everyone respond", mention_all=True
    )
    assert all_submitted.turn is not None
    assert {target.employee_id for target in all_submitted.turn.targets} == {
        "employee-a",
        "employee-b",
    }
    snapshot = store.snapshot_payload(group.group_id)
    assert snapshot["group"]["last_sequence"] == all_submitted.event.sequence
    assert [event["sequence"] for event in snapshot["events"]] == list(
        range(1, all_submitted.event.sequence + 1)
    )
    incremental = store.snapshot_payload(
        group.group_id, after_sequence=submitted.event.sequence
    )
    assert [event["event_id"] for event in incremental["events"]] == [
        all_submitted.event.event_id
    ]
    assert len(incremental["memberships"]) == 2
    assert len(incremental["turns"]) == 2
    assert len(incremental["targets"]) == 3
    assert incremental["reconciliation"] == {
        "after_sequence": submitted.event.sequence,
        "last_sequence": all_submitted.event.sequence,
        "next_after_sequence": all_submitted.event.sequence,
        "snapshot_authoritative": True,
    }
    page = store.list_events_payload(
        group.group_id, after_sequence=submitted.event.sequence, limit=1
    )
    assert len(page["events"]) == 1
    assert page["has_more"] is False
    assert page["next_after_sequence"] == all_submitted.event.sequence


def test_message_submission_rolls_back_on_invalid_mention_or_attachment(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group("Atomic", members=[_member("employee-a")])
    before = store.get_group(group.group_id).last_sequence

    membership = store.active_memberships(group.group_id)[0]
    with pytest.raises(RuntimeError, match="mentioned"):
        store.submit_owner_message(
            group.group_id, text="Hello", mentioned_membership_ids=["missing"]
        )
    with pytest.raises(RuntimeError, match="attachment"):
        store.submit_owner_message(
            group.group_id, text="Hello",
            mentioned_membership_ids=[membership.membership_id],
            attachment_ids=["missing"],
        )

    other_group = store.create_group("Other", members=[_member("employee-a")])
    other_attachment = store.create_attachment(
        other_group.group_id,
        filename="other.txt",
        media_type="text/plain",
        size_bytes=5,
        storage_key="groups/other/file",
        content_sha256="c" * 64,
    )
    with pytest.raises(RuntimeError, match="attachment"):
        store.submit_owner_message(
            group.group_id,
            text="Cross-group",
            mentioned_membership_ids=[membership.membership_id],
            attachment_ids=[other_attachment["attachment_id"]],
        )

    assert store.get_group(group.group_id).last_sequence == before
    with db._lock:
        assert db._conn.execute(
            "SELECT COUNT(*) FROM collaboration_events WHERE group_id=?",
            (group.group_id,),
        ).fetchone()[0] == before


def test_attachment_grants_do_not_expand_to_late_members(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group("Late", members=[_member("employee-a")])
    first = store.active_memberships(group.group_id)[0]
    attachment = store.create_attachment(
        group.group_id,
        filename="private.txt",
        media_type="text/plain",
        size_bytes=7,
        storage_key="groups/late/private",
        content_sha256="d" * 64,
    )
    submitted = store.submit_owner_message(
        group.group_id,
        text="Only current target",
        mentioned_membership_ids=[first.membership_id],
        attachment_ids=[attachment["attachment_id"]],
    )
    store.add_membership(group.group_id, _member("employee-b"))

    with db._lock:
        grant = db._conn.execute(
            "SELECT ag.target_id FROM collaboration_attachment_grants ag "
            "WHERE ag.attachment_id=?",
            (attachment["attachment_id"],),
        ).fetchall()
    assert [row["target_id"] for row in grant] == [submitted.turn.targets[0].target_id]


def test_message_submission_idempotency_replays_exact_request_only(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group("Idempotent", members=[_member("employee-a")])
    membership = store.active_memberships(group.group_id)[0]

    first = store.submit_owner_message(
        group.group_id,
        text="Run once",
        mentioned_membership_ids=[membership.membership_id],
        client_idempotency_key="browser-message-1",
    )
    replay = store.submit_owner_message(
        group.group_id,
        text="Run once",
        mentioned_membership_ids=[membership.membership_id],
        client_idempotency_key="browser-message-1",
    )

    assert replay.event.event_id == first.event.event_id
    assert replay.turn is not None and first.turn is not None
    assert replay.turn.turn_id == first.turn.turn_id
    assert replay.turn.targets[0].target_id == first.turn.targets[0].target_id
    with pytest.raises(RuntimeError, match="idempotency key request mismatch"):
        store.submit_owner_message(
            group.group_id,
            text="Different request",
            mentioned_membership_ids=[membership.membership_id],
            client_idempotency_key="browser-message-1",
        )
    with db._lock:
        assert db._conn.execute(
            "SELECT COUNT(*) FROM collaboration_message_receipts"
        ).fetchone()[0] == 1
        assert db._conn.execute(
            "SELECT COUNT(*) FROM collaboration_turn_targets"
        ).fetchone()[0] == 1


def test_employee_events_require_consistent_membership_identity(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group("Replies", members=[_member("employee-a")])
    membership = store.snapshot_payload(group.group_id)["memberships"][0]

    with db._lock:
        now = 123.0
        row = store._append_event(
            db._conn,
            group_id=group.group_id,
            event_kind="message.employee",
            actor_kind="employee",
            actor_employee_id="employee-a",
            actor_membership_id=membership["membership_id"],
            body={"text": "done"},
            now=now,
        )
    event = store._event(row)
    assert event.actor_employee_id == "employee-a"
    assert event.actor_membership_id == membership["membership_id"]
    assert store.list_events_payload(group.group_id)["events"][-1][
        "actor_membership_id"
    ] == membership["membership_id"]

    with pytest.raises(RuntimeError, match="inconsistent"):
        with db._lock:
            store._append_event(
                db._conn,
                group_id=group.group_id,
                event_kind="message.employee",
                actor_kind="employee",
                actor_employee_id="employee-b",
                actor_membership_id=membership["membership_id"],
                body={},
                now=124.0,
            )


def test_ai_round_dispatch_is_atomic_bounded_and_idempotent(db):
    store = CollaborationStore(db, owner_key="owner-a")
    source = store.create_group("Source")
    selected = store.create_attachment(
        source.group_id,
        filename="selected.txt",
        media_type="text/plain",
        size_bytes=8,
        storage_key="groups/source/selected",
        content_sha256="e" * 64,
    )
    store.create_attachment(
        source.group_id,
        filename="not-selected.txt",
        media_type="text/plain",
        size_bytes=12,
        storage_key="groups/source/not-selected",
        content_sha256="f" * 64,
    )
    created, _ = store.create_ai_task(
        title="AI review",
        brief="Review explicitly",
        creator=_member("creator"),
        members=[_member("employee-a")],
        source_kind="web_group",
        source_conversation_id=source.group_id,
        source_group_id=source.group_id,
        source_event_id=None,
        source_task_id=None,
        depth=1,
        allowed_attachment_ids=[selected["attachment_id"]],
        idempotency_key="create-a",
    )
    transferred_attachment_id = created["allowed_attachment_ids"][0]

    first, first_round, first_created = store.dispatch_ai_round(
        created["task_id"],
        instruction="Round one @text-only",
        target_employee_ids=["employee-a"],
        attachment_ids=[transferred_attachment_id],
        idempotency_key="dispatch-a",
    )
    replay, replay_round, replay_created = store.dispatch_ai_round(
        created["task_id"],
        instruction="Round one @text-only",
        target_employee_ids=["employee-a"],
        attachment_ids=[transferred_attachment_id],
        idempotency_key="dispatch-a",
    )

    assert (first_round, replay_round) == (1, 1)
    assert first_created is True and replay_created is False
    assert replay.event.event_id == first.event.event_id
    assert replay.turn.turn_id == first.turn.turn_id
    assert len(replay.turn.targets) == 1
    with db._lock:
        target_count = db._conn.execute(
            "SELECT COUNT(*) FROM collaboration_turn_targets"
        ).fetchone()[0]
        round_value = db._conn.execute(
            "SELECT round FROM collaboration_tasks WHERE task_id=?",
            (created["task_id"],),
        ).fetchone()[0]
        transferred = db._conn.execute(
            "SELECT filename, storage_key FROM collaboration_attachments "
            "WHERE group_id=?",
            (created["group_id"],),
        ).fetchall()
        grants = db._conn.execute(
            "SELECT attachment_id FROM collaboration_attachment_grants"
        ).fetchall()
    assert (target_count, round_value) == (1, 1)
    assert [tuple(row) for row in transferred] == [
        ("selected.txt", "groups/source/selected")
    ]
    assert [row["attachment_id"] for row in grants] == [
        transferred_attachment_id
    ]

    with pytest.raises(RuntimeError, match="request mismatch"):
        store.dispatch_ai_round(
            created["task_id"],
            instruction="different",
            target_employee_ids=["employee-a"],
            attachment_ids=[],
            idempotency_key="dispatch-a",
        )

    for round_number in (2, 3):
        with db._lock:
            db._conn.execute(
                "UPDATE collaboration_turns SET status='completed' WHERE group_id=?",
                (created["group_id"],),
            )
            db._conn.commit()
        dispatched, actual_round, was_created = store.dispatch_ai_round(
            created["task_id"],
            instruction=f"Round {round_number}",
            target_employee_ids=["employee-a"],
            attachment_ids=[],
            idempotency_key=f"dispatch-{round_number}",
        )
        assert dispatched.turn is not None
        assert actual_round == round_number
        assert was_created is True

    with db._lock:
        db._conn.execute(
            "UPDATE collaboration_turns SET status='completed' WHERE group_id=?",
            (created["group_id"],),
        )
        db._conn.commit()
    with pytest.raises(RuntimeError, match="round limit reached"):
        store.dispatch_ai_round(
            created["task_id"],
            instruction="Forbidden fourth round",
            target_employee_ids=["employee-a"],
            attachment_ids=[],
            idempotency_key="dispatch-4",
        )


def test_events_are_append_only_and_worker_tables_have_complete_fences(db):
    store = CollaborationStore(db, owner_key="owner-a")
    group = store.create_group("Audit")
    event_id = store.list_events_payload(group.group_id)["events"][0]["event_id"]

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db._conn.execute(
            "UPDATE collaboration_events SET event_kind='changed' WHERE event_id=?",
            (event_id,),
        )
    fence_columns = {
        "worker_owner_key",
        "worker_id",
        "worker_generation",
        "lease_version",
        "recovery_generation",
    }
    for table in (
        "collaboration_tasks",
        "collaboration_turns",
        "collaboration_turn_targets",
        "collaboration_attachment_materializations",
        "collaboration_approvals",
        "collaboration_tool_receipts",
        "collaboration_delivery_state",
    ):
        columns = {
            row["name"] for row in db._conn.execute(f"PRAGMA table_info({table})")
        }
        assert fence_columns <= columns


def test_state_machine_schema_has_durable_scheduler_fields(db):
    target_columns = {
        row["name"]
        for row in db._conn.execute("PRAGMA table_info(collaboration_turn_targets)")
    }
    assert {
        "execution_id",
        "error",
        "result_json",
        "last_delivered_sequence",
        "active_seconds",
        "active_started_at",
        "attempt",
    } <= target_columns
    turn_sql = db._conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='collaboration_turns'"
    ).fetchone()["sql"]
    for status in (
        "queued",
        "running",
        "completed",
        "partial",
        "failed",
        "ambiguous",
        "cancelled",
    ):
        assert status in turn_sql
    target_sql = db._conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='collaboration_turn_targets'"
    ).fetchone()["sql"]
    for status in (
        "queued",
        "running",
        "waiting_approval",
        "completed",
        "failed",
        "timed_out",
        "ambiguous",
        "cancelled",
    ):
        assert status in target_sql

    task_columns = {
        row["name"] for row in db._conn.execute("PRAGMA table_info(collaboration_tasks)")
    }
    origin_columns = {
        row["name"] for row in db._conn.execute("PRAGMA table_info(collaboration_origins)")
    }
    assert {
        "source_kind",
        "round",
        "depth",
        "source_event_id",
        "source_task_id",
    } <= task_columns
    assert {
        "source_kind",
        "round",
        "depth",
        "source_event_id",
        "source_task_id",
    } <= origin_columns
    attachment_columns = {
        row["name"]
        for row in db._conn.execute("PRAGMA table_info(collaboration_attachments)")
    }
    assert "storage_key" in attachment_columns
    assert "source_path" not in attachment_columns
