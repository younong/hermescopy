from types import SimpleNamespace
import zipfile

from agent.turn_finalizer import finalize_turn


class FakeAgent:
    def __init__(self):
        self.max_iterations = 90
        self.iteration_budget = SimpleNamespace(remaining=10, used=1, max_total=90)
        self.quiet_mode = True
        self.model = "test-model"
        self.provider = "test-provider"
        self.base_url = ""
        self.session_id = "sess-test"
        self.context_compressor = SimpleNamespace(last_prompt_tokens=0)
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0
        self.session_reasoning_tokens = 0
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0
        self.session_total_tokens = 0
        self.session_estimated_cost_usd = 0
        self.session_cost_status = "unknown"
        self.session_cost_source = "test"
        self._tool_guardrail_halt_decision = None
        self._interrupt_message = None
        self._response_was_previewed = True
        self._session_visibility = "visible"
        self._turn_failed_file_mutations = {}
        self._skill_nudge_interval = 0
        self._iters_since_skill = 0
        self.valid_tool_names = []
        self.persisted_messages = None

    def _handle_max_iterations(self, messages, api_call_count):
        raise AssertionError("not expected")

    def _emit_status(self, *_args, **_kwargs):
        pass

    def _safe_print(self, *_args, **_kwargs):
        pass

    def _save_trajectory(self, *_args, **_kwargs):
        pass

    def _cleanup_task_resources(self, *_args, **_kwargs):
        pass

    def _drop_trailing_empty_response_scaffolding(self, messages):
        pass

    def _persist_session(self, messages, conversation_history):
        self.persisted_messages = list(messages)

    def _file_mutation_verifier_enabled(self):
        return False

    def _format_file_mutation_failure_footer(self, _failed):
        return "⚠️ File-mutation verifier: internal diagnostic"

    def _turn_completion_explainer_enabled(self):
        return False

    def _drain_pending_steer(self):
        return None

    def clear_interrupt(self):
        pass

    def _sync_external_memory_for_turn(self, **_kwargs):
        pass


def test_internal_collaboration_response_hides_file_mutation_diagnostics(monkeypatch):
    """Internal collaboration turns must not leak verifier details to members."""
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    agent._session_visibility = "internal"
    agent._turn_failed_file_mutations = {
        "/tmp/group_chat_reply.txt": {
            "tool": "write_file",
            "error_preview": "path must be workspace-relative",
        }
    }
    agent._file_mutation_verifier_enabled = lambda: True
    messages = [
        {"role": "user", "content": "哈哈"},
        {"role": "assistant", "content": "Done."},
    ]

    result = finalize_turn(
        agent,
        final_response="Done.",
        api_call_count=2,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="collaboration-task",
        turn_id="turn",
        user_message="哈哈",
        original_user_message="哈哈",
        _should_review_memory=False,
        _turn_exit_reason="text_response(stop)",
    )

    assert result["final_response"] == "Done."
    assert "File-mutation verifier" not in result["final_response"]
    assert "/tmp/group_chat_reply.txt" not in result["final_response"]
    assert agent.persisted_messages[-1]["content"] == "Done."


def test_final_response_closes_tool_tail_before_persistence(monkeypatch):
    """A recovered/previewed final response must be durable in session history.

    Regression for turns where the caller receives a non-empty final_response,
    but the message transcript still ends at a tool result. If persisted that
    way, the next turn reloads a stale/malformed history and can appear to loop
    because the assistant's visible final answer is missing from durable state.
    """
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    messages = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "I'll check.",
            "tool_calls": [
                {"id": "call-1", "function": {"name": "terminal", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "name": "terminal", "content": "ok"},
    ]

    result = finalize_turn(
        agent,
        final_response="Done.",
        api_call_count=2,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="do it",
        original_user_message="do it",
        _should_review_memory=False,
        _turn_exit_reason="fallback_prior_turn_content",
    )

    assert result["messages"][-1] == {"role": "assistant", "content": "Done."}
    assert agent.persisted_messages is not None
    assert agent.persisted_messages[-1] == {"role": "assistant", "content": "Done."}


def test_invalid_declared_artifact_warning_is_persisted(monkeypatch, tmp_path):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    (tmp_path / "project").mkdir()
    agent = FakeAgent()
    messages = [
        {"role": "user", "content": "build it"},
        {"role": "assistant", "content": "生成文件：`project`"},
    ]

    result = finalize_turn(
        agent,
        final_response="生成文件：`project`",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="artifact-test",
        turn_id="turn",
        user_message="build it",
        original_user_message="build it",
        _should_review_memory=False,
        _turn_exit_reason="text_response(stop)",
    )

    assert result["artifacts"] == []
    assert "没有生成下载卡片" in result["final_response"]
    assert agent.persisted_messages[-1]["content"] == result["final_response"]
    assert result["messages"][-1]["content"] == result["final_response"]


def test_transformed_response_is_validated_persisted_and_posted_consistently(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    (tmp_path / "directory").mkdir()
    posted = []

    def invoke(name, **kwargs):
        if name == "transform_llm_output":
            return ["生成文件：`directory`"]
        if name == "post_llm_call":
            posted.append(kwargs)
        return []

    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", invoke)
    agent = FakeAgent()
    messages = [
        {"role": "user", "content": "build it"},
        {"role": "assistant", "content": "Done."},
    ]

    result = finalize_turn(
        agent,
        final_response="Done.",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="artifact-test",
        turn_id="turn",
        user_message="build it",
        original_user_message="build it",
        _should_review_memory=False,
        _turn_exit_reason="text_response(stop)",
    )

    assert result["response_transformed"] is True
    assert result["artifacts"] == []
    assert "没有生成下载卡片" in result["final_response"]
    assert agent.persisted_messages[-1]["content"] == result["final_response"]
    assert posted[0]["assistant_response"] == result["final_response"]
    assert posted[0]["conversation_history"][-1]["content"] == result["final_response"]


def test_unexpected_validation_error_fails_closed(monkeypatch):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setattr(
        "agent.artifact_delivery.validate_declared_artifacts",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("validator offline")),
    )
    agent = FakeAgent()
    messages = [
        {"role": "user", "content": "build it"},
        {"role": "assistant", "content": "[下载](tool.zip)"},
    ]

    result = finalize_turn(
        agent,
        final_response="[下载](tool.zip)",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="artifact-test",
        turn_id="turn",
        user_message="build it",
        original_user_message="build it",
        _should_review_memory=False,
        _turn_exit_reason="text_response(stop)",
    )

    assert result["artifacts"] == []
    assert "交付校验暂时不可用" in result["final_response"]
    assert agent.persisted_messages[-1]["content"] == result["final_response"]


def test_exhausted_zip_gate_does_not_deliver_loose_files(monkeypatch, tmp_path):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    agent = FakeAgent()
    messages = [
        {"role": "user", "content": "请给我 zip"},
        {"role": "assistant", "content": "[a](a.txt)\n[b](b.txt)"},
    ]

    result = finalize_turn(
        agent,
        final_response="[a](a.txt)\n[b](b.txt)",
        api_call_count=3,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="artifact-test",
        turn_id="turn",
        user_message="请给我 zip",
        original_user_message="请给我 zip",
        _should_review_memory=False,
        _turn_exit_reason="text_response(stop)",
    )

    assert result["artifacts"] == []
    assert "没有生成下载卡片" in result["final_response"]
    assert "attachments" not in agent.persisted_messages[-1]


def test_valid_declared_artifact_is_returned_for_gateway(monkeypatch, tmp_path):
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    archive = tmp_path / "tool.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("result.txt", "archive")
    agent = FakeAgent()
    messages = [
        {"role": "user", "content": "build it"},
        {"role": "assistant", "content": "[下载](tool.zip)"},
    ]

    result = finalize_turn(
        agent,
        final_response="[下载](tool.zip)",
        api_call_count=1,
        interrupted=False,
        failed=False,
        messages=messages,
        conversation_history=[],
        effective_task_id="artifact-test",
        turn_id="turn",
        user_message="build it",
        original_user_message="build it",
        _should_review_memory=False,
        _turn_exit_reason="text_response(stop)",
    )

    assert result["artifacts"][0]["path"] == str(archive)
    assert result["artifacts"][0]["name"] == "tool.zip"
    assert result["artifacts"][0]["size_bytes"] == archive.stat().st_size
    assert agent.persisted_messages[-1]["attachments"] == [
        {
            "kind": "file",
            "mime_type": "application/zip",
            "name": "tool.zip",
            "path": str(archive),
            "size_bytes": archive.stat().st_size,
        }
    ]


def test_failed_final_response_is_durable_with_error_metadata(monkeypatch):
    """Terminal provider failures use the same durable finalization path."""
    monkeypatch.setattr("hermes_cli.plugins.invoke_hook", lambda *_a, **_kw: [])
    agent = FakeAgent()
    messages = [{"role": "user", "content": "try again"}]

    result = finalize_turn(
        agent,
        final_response="API call failed after 3 retries: 502 Bad Gateway",
        api_call_count=1,
        interrupted=False,
        failed=True,
        error="502 Bad Gateway",
        failure_reason="server_error",
        messages=messages,
        conversation_history=[],
        effective_task_id="task",
        turn_id="turn",
        user_message="try again",
        original_user_message="try again",
        _should_review_memory=False,
        _turn_exit_reason="api_retries_exhausted",
    )

    expected = {
        "role": "assistant",
        "content": "API call failed after 3 retries: 502 Bad Gateway",
    }
    assert result["failed"] is True
    assert result["completed"] is False
    assert result["error"] == "502 Bad Gateway"
    assert result["failure_reason"] == "server_error"
    assert result["turn_exit_reason"] == "api_retries_exhausted"
    assert result["messages"][-1] == expected
    assert agent.persisted_messages[-1] == expected
