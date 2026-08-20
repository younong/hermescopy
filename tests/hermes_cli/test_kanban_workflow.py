from hermes_cli import kanban_db as kb


def test_workflow_hands_off_to_next_profile(kanban_home):
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="handoff",
            workflow={"steps": [
                {"key": "analysis", "assignee": "alpha"},
                {"key": "implementation", "assignee": "beta"},
                {"key": "review", "assignee": "gamma"},
            ]},
        )
        assert kb.complete_task(conn, task_id, summary="analysis complete")
        task = kb.get_task(conn, task_id)
        assert (task.status, task.assignee, task.current_step_key) == (
            "ready", "beta", "implementation"
        )
        assert [event.kind for event in kb.list_events(conn, task_id)].count("workflow_advanced") == 1
        assert kb.complete_task(conn, task_id, summary="implementation complete")
        task = kb.get_task(conn, task_id)
        assert (task.status, task.assignee, task.current_step_key) == (
            "ready", "gamma", "review"
        )
        assert kb.complete_task(conn, task_id, summary="review complete")
        assert kb.get_task(conn, task_id).status == "done"
        assert any(event.kind == "workflow_completed" for event in kb.list_events(conn, task_id))


def test_invalid_workflow_is_rejected(kanban_home):
    with kb.connect() as conn:
        try:
            kb.create_task(conn, title="bad", workflow={"steps": [{"key": "x"}]})
        except ValueError as exc:
            assert "key and assignee" in str(exc)
        else:
            raise AssertionError("invalid workflow was accepted")
