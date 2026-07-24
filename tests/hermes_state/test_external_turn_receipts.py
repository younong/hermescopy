"""Tests for at-most-once external Agent turn receipts."""

from __future__ import annotations

from hermes_state import SessionDB


def test_external_turn_completed_receipt_replays_result(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")

    claimed = db.begin_external_turn(
        turn_key="turn-1",
        stored_session_id="session-1",
        worker_id="worker-1",
        worker_generation=1,
    )
    db.complete_external_turn(
        turn_key="turn-1",
        worker_id="worker-1",
        worker_generation=1,
        result_text="answer",
        result_status="complete",
    )
    replay = db.begin_external_turn(
        turn_key="turn-1",
        stored_session_id="session-1",
        worker_id="worker-1",
        worker_generation=1,
    )

    assert claimed == {"status": "claimed"}
    assert replay["status"] == "completed"
    assert replay["result_text"] == "answer"


def test_external_turn_from_replaced_generation_becomes_ambiguous(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.begin_external_turn(
        turn_key="turn-1",
        stored_session_id="session-1",
        worker_id="worker-1",
        worker_generation=1,
    )

    result = db.begin_external_turn(
        turn_key="turn-1",
        stored_session_id="session-1",
        worker_id="worker-2",
        worker_generation=2,
    )

    assert result["status"] == "ambiguous"
