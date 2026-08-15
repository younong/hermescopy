import concurrent.futures
import time

from hermes_state import SessionDB


OWNER = "ok1_owner"
WORKSPACE = "/workspace/owner"
EMPLOYEE = "employee-a"


def _scope(*, owner=OWNER, workspace=WORKSPACE, generation=2):
    return {
        "owner_key": owner,
        "workspace_root": workspace,
        "worker_generation": generation,
    }


def _claim(db, proposed, *, owner=OWNER, workspace=WORKSPACE, employee=EMPLOYEE, generation=2):
    return db.claim_web_direct_employee_conversation(
        owner_key=owner,
        workspace_root=workspace,
        employee_id=employee,
        proposed_session_id=proposed,
        worker_generation=generation,
    )


def _persist_employee_session(db, session_id, *, owner=OWNER, workspace=WORKSPACE, employee=EMPLOYEE, generation=2, parent=None):
    db.create_session(
        session_id,
        source="dashboard-gui",
        owner_key=owner,
        workspace_root=workspace,
        worker_generation=generation,
        parent_session_id=parent,
        model_config={
            "hermes_web_direct_employee_id": employee,
            "hermes_employee_policy": {"employee_id": employee},
        },
    )


def _binding_row(db, *, employee=EMPLOYEE):
    row = db._conn.execute(
        """SELECT * FROM web_direct_employee_conversations
           WHERE owner_key = ? AND workspace_root = ? AND employee_id = ?""",
        (OWNER, WORKSPACE, employee),
    ).fetchone()
    return dict(row) if row else None


def test_same_employee_scope_returns_one_reservation(tmp_path):
    db = SessionDB(tmp_path / "state.db")

    first = _claim(db, "first")
    second = _claim(db, "second")

    assert first["root_session_id"] == "first"
    assert first["state"] == "reserved"
    assert second["root_session_id"] == "first"
    assert second["binding_epoch"] == first["binding_epoch"]


def test_concurrent_claims_converge_on_one_reservation(tmp_path):
    path = tmp_path / "state.db"
    SessionDB(path).close()

    def claim(index):
        db = SessionDB(path)
        try:
            return _claim(db, f"proposed-{index}")
        finally:
            db.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        bindings = list(pool.map(claim, range(8)))

    assert len({binding["root_session_id"] for binding in bindings}) == 1
    assert len({binding["binding_epoch"] for binding in bindings}) == 1


def test_employee_bindings_are_isolated_by_owner_workspace_and_employee(tmp_path):
    db = SessionDB(tmp_path / "state.db")

    assert _claim(db, "owner-a")["root_session_id"] == "owner-a"
    assert _claim(db, "owner-b", owner="ok1_other")["root_session_id"] == "owner-b"
    assert _claim(db, "workspace-b", workspace="/workspace/other")["root_session_id"] == "workspace-b"
    assert _claim(db, "employee-b", employee="employee-b")["root_session_id"] == "employee-b"


def test_first_claim_adopts_latest_legacy_employee_session(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _persist_employee_session(db, "older", generation=1)
    _persist_employee_session(db, "newer", generation=1)
    db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = 'older'", (time.time() - 100,))
    db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = 'newer'", (time.time(),))
    db._conn.commit()

    binding = _claim(db, "unused")

    assert binding["root_session_id"] == "newer"
    assert binding["state"] == "adopted"
    assert binding["reservation_worker_generation"] is None


def test_legacy_adoption_excludes_employee_branches(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _persist_employee_session(db, "root", generation=1)
    db.create_session(
        "branch",
        source="dashboard-gui",
        owner_key=OWNER,
        workspace_root=WORKSPACE,
        worker_generation=1,
        parent_session_id="root",
        model_config={
            "hermes_web_direct_employee_id": EMPLOYEE,
            "hermes_employee_policy": {"employee_id": EMPLOYEE},
            "_branched_from": "root",
        },
    )
    db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = 'branch'", (time.time(),))
    db._conn.commit()

    binding = _claim(db, "unused")

    assert binding["root_session_id"] == "root"


def test_tombstone_prevents_legacy_rediscovery(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    _persist_employee_session(db, "legacy", generation=1)
    binding = _claim(db, "unused")
    assert db.delete_session("legacy", recovery_scope={**_scope(), "historical_resume": True})

    replacement = _claim(db, "replacement")

    assert replacement["root_session_id"] == "replacement"
    assert replacement["binding_epoch"] == binding["binding_epoch"] + 1


def test_empty_reservation_survives_same_worker_and_rotates_after_restart(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    first = _claim(db, "draft-a", generation=2)

    same_worker = _claim(db, "draft-b", generation=2)
    restarted = _claim(db, "draft-c", generation=3)

    assert same_worker["root_session_id"] == "draft-a"
    assert restarted["root_session_id"] == "draft-c"
    assert restarted["binding_epoch"] == first["binding_epoch"] + 1


def test_persisted_binding_survives_worker_restart_and_resolves_compression_tip(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    binding = _claim(db, "root", generation=1)
    _persist_employee_session(db, "root", generation=1)
    db.append_message("root", role="user", content="before compression")
    db.end_session("root", "compression")
    _persist_employee_session(db, "tip", generation=2, parent="root")
    db.append_message("tip", role="assistant", content="after compression")

    reopened = _claim(db, "unused", generation=3)
    historical_scope = {**_scope(generation=3), "historical_resume": True}

    assert reopened["root_session_id"] == "root"
    assert reopened["binding_epoch"] == binding["binding_epoch"]
    assert db.resolve_resume_session_id("root", recovery_scope=historical_scope) == "tip"


def test_branch_is_not_part_of_bound_compression_lineage(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    binding = _claim(db, "root")
    _persist_employee_session(db, "root")
    db.create_session(
        "branch",
        source="dashboard-gui",
        parent_session_id="root",
        model_config={"_branched_from": "root"},
        **_scope(),
    )

    assert db.delete_session("branch", recovery_scope=_scope())
    current = _binding_row(db)

    assert current["root_session_id"] == "root"
    assert current["binding_epoch"] == binding["binding_epoch"]
    assert current["tombstoned"] == 0


def test_archive_keeps_employee_binding(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    binding = _claim(db, "root")
    _persist_employee_session(db, "root")

    assert db.set_session_archived("root", True, recovery_scope=_scope())
    reopened = _claim(db, "unused")

    assert reopened["root_session_id"] == "root"
    assert reopened["binding_epoch"] == binding["binding_epoch"]


def test_deleting_compression_tip_tombstones_root_binding(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    binding = _claim(db, "root", generation=1)
    _persist_employee_session(db, "root", generation=1)
    db.end_session("root", "compression")
    _persist_employee_session(db, "tip", generation=2, parent="root")

    historical_scope = {**_scope(), "historical_resume": True}
    assert db.delete_session("tip", recovery_scope=historical_scope)
    tombstone = _binding_row(db)

    assert tombstone["root_session_id"] is None
    assert tombstone["tombstoned"] == 1
    assert tombstone["binding_epoch"] == binding["binding_epoch"] + 1


def test_bulk_delete_tombstones_each_matching_binding(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    first = _claim(db, "employee-a-root", employee="employee-a")
    second = _claim(db, "employee-b-root", employee="employee-b")
    _persist_employee_session(db, "employee-a-root", employee="employee-a")
    _persist_employee_session(db, "employee-b-root", employee="employee-b")

    assert db.delete_sessions(
        ["employee-a-root", "employee-b-root"], recovery_scope=_scope()
    ) == 2

    for employee, binding in (("employee-a", first), ("employee-b", second)):
        row = _binding_row(db, employee=employee)
        assert row["tombstoned"] == 1
        assert row["binding_epoch"] == binding["binding_epoch"] + 1


def test_all_session_cleanup_paths_tombstone_employee_binding(tmp_path):
    for cleanup in ("delete_if_empty", "delete_empty", "prune"):
        db = SessionDB(tmp_path / f"{cleanup}.db")
        binding = _claim(db, "root")
        _persist_employee_session(db, "root")

        if cleanup == "delete_if_empty":
            assert db.delete_session_if_empty("root")
        else:
            db.end_session("root", "closed")
            if cleanup == "delete_empty":
                assert db.delete_empty_sessions(recovery_scope=_scope()) == 1
            else:
                db._conn.execute(
                    "UPDATE sessions SET started_at = ? WHERE id = 'root'",
                    (time.time() - 100 * 86400,),
                )
                db._conn.commit()
                assert db.prune_sessions(older_than_days=90, recovery_scope=_scope()) == 1

        tombstone = _binding_row(db)
        assert tombstone["tombstoned"] == 1
        assert tombstone["binding_epoch"] == binding["binding_epoch"] + 1
        db.close()


def test_stale_binding_epoch_fails_closed(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    binding = _claim(db, "root")
    _persist_employee_session(db, "root")
    assert db.delete_session("root", recovery_scope=_scope())
    _claim(db, "replacement")

    assert not db.web_direct_employee_binding_matches(
        owner_key=OWNER,
        workspace_root=WORKSPACE,
        employee_id=EMPLOYEE,
        root_session_id="root",
        binding_epoch=binding["binding_epoch"],
    )
    current = _binding_row(db)
    assert db.web_direct_employee_binding_matches(
        owner_key=OWNER,
        workspace_root=WORKSPACE,
        employee_id=EMPLOYEE,
        root_session_id="replacement",
        binding_epoch=current["binding_epoch"],
    )
