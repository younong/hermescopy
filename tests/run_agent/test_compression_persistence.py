"""Tests for context compression persistence in the gateway.

Verifies that when context compression fires during run_conversation(),
the compressed messages are properly persisted to both SQLite (via the
agent) and JSONL (via the gateway).

Bug scenario (pre-fix):
  1. Gateway loads 200-message history, passes to agent
  2. Agent's run_conversation() compresses to ~30 messages mid-run
  3. _compress_context() resets _last_flushed_db_idx = 0
  4. On exit, _flush_messages_to_session_db() calculates:
     flush_from = max(len(conversation_history=200), _last_flushed_db_idx=0) = 200
  5. messages[200:] is empty (only ~30 messages after compression)
  6. Nothing written to new session's SQLite — compressed context lost
  7. Gateway's history_offset was still 200, producing empty new_messages
  8. Fallback wrote only user/assistant pair — summary lost
"""

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.prepared_model_request import PreparedModelRequest, PreparedRequestAccounting


def _prepared(tokens, *, label="request"):
    return PreparedModelRequest(
        request_id=label,
        route=SimpleNamespace(
            provider="test",
            model="test/model",
            base_url="http://test",
            api_mode="chat_completions",
        ),
        payload={"messages": [{"role": "user", "content": label}]},
        original_payload={},
        middleware_trace=(),
        accounting=PreparedRequestAccounting(
            raw_input_tokens=tokens,
            effective_input_tokens=tokens,
            output_token_limit=10,
            context_limit=200,
            compression_threshold=100,
            hard_input_limit=190,
            categories=(),
        ),
        message_count=1,
        tool_count=0,
        request_char_count=len(label),
    )


# ---------------------------------------------------------------------------
# Part 1: Agent-side — _flush_messages_to_session_db after compression
# ---------------------------------------------------------------------------

class TestFlushAfterCompression:
    """Verify that compressed messages are flushed to the new session's SQLite
    even when conversation_history (from the original session) is longer than
    the compressed messages list."""

    def _make_agent(self, session_db):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            from run_agent import AIAgent
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                session_db=session_db,
                session_id="original-session",
                skip_context_files=True,
                skip_memory=True,
            )
        return agent

    def test_flush_after_compression_with_long_history(self):
        """The actual bug: conversation_history longer than compressed messages.

        Before the fix, flush_from = max(len(conversation_history), 0) = 200,
        but messages only has ~30 entries, so messages[200:] is empty.
        After the fix, conversation_history is cleared to None after compression,
        so flush_from = max(0, 0) = 0, and ALL compressed messages are written.
        """
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = SessionDB(db_path=db_path)

            agent = self._make_agent(db)

            # Simulate the original long history (200 messages)
            original_history = [
                {"role": "user" if i % 2 == 0 else "assistant",
                 "content": f"message {i}"}
                for i in range(200)
            ]

            # First, flush original messages to the original session
            agent._flush_messages_to_session_db(original_history, [])
            original_rows = db.get_messages("original-session")
            assert len(original_rows) == 200

            # Now simulate compression: new session, reset idx, shorter messages
            agent.session_id = "compressed-session"
            db.create_session(session_id="compressed-session", source="test")
            agent._last_flushed_db_idx = 0

            # The compressed messages (summary + tail + new turn)
            compressed_messages = [
                {"role": "user", "content": "[CONTEXT COMPACTION] Summary of work..."},
                {"role": "user", "content": "What should we do next?"},
                {"role": "assistant", "content": "Let me check..."},
                {"role": "user", "content": "new question"},
                {"role": "assistant", "content": "new answer"},
            ]

            # THE BUG: passing the original history as conversation_history
            # causes flush_from = max(200, 0) = 200, skipping everything.
            # After the fix, conversation_history should be None.
            agent._flush_messages_to_session_db(compressed_messages, None)

            new_rows = db.get_messages("compressed-session")
            assert len(new_rows) == 5, (
                f"Expected 5 compressed messages in new session, got {len(new_rows)}. "
                f"Compression persistence bug: messages not written to SQLite."
            )

    def test_flush_with_stale_history_loses_messages(self):
        """Stale conversation_history no longer causes data loss."""
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = SessionDB(db_path=db_path)

            agent = self._make_agent(db)

            # Simulate compression reset
            agent.session_id = "new-session"
            db.create_session(session_id="new-session", source="test")
            agent._last_flushed_db_idx = 0

            compressed = [
                {"role": "user", "content": "summary"},
                {"role": "assistant", "content": "continuing..."},
            ]

            # Stale history longer than messages: the old positional flush
            # sliced past the end and dropped both messages (#46053).
            stale_history = [{"role": "user", "content": f"msg{i}"} for i in range(100)]
            agent._flush_messages_to_session_db(compressed, stale_history)

            rows = db.get_messages("new-session")
            assert len(rows) == 2
            assert [row["content"] for row in rows] == ["summary", "continuing..."]

    def test_in_place_compression_rebaseline_prevents_duplicate_compacted_rows(self):
        """In-place compaction already persisted the compacted transcript.

        Regression for the 2026-06-26 SRE compression loop: archive_and_compact()
        inserted a compacted active block, then the same turn continued with
        conversation_history=None and _flush_messages_to_session_db() appended
        the compacted dicts again, doubling live context.
        """
        from agent.conversation_compression import conversation_history_after_compression
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "test.db"
            db = SessionDB(db_path=db_path)

            agent = self._make_agent(db)
            agent._ensure_db_session()

            original_history = [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ]
            agent._flush_messages_to_session_db(original_history, [])
            assert [row["content"] for row in db.get_messages("original-session")] == [
                "old question",
                "old answer",
            ]

            compacted = [
                {"role": "assistant", "content": "[CONTEXT COMPACTION] summary"},
                {"role": "user", "content": "recent question"},
                {"role": "assistant", "content": "recent answer"},
            ]
            db.archive_and_compact("original-session", compacted)
            setattr(agent, "_last_compaction_in_place", True)
            agent._last_flushed_db_idx = 0

            # Same agent turn continues after compaction. The compacted dicts
            # must be treated as already-persisted history; only later appends
            # should be flushed.
            post_compaction_history = conversation_history_after_compression(
                agent, compacted
            )
            assert post_compaction_history is not None
            assert post_compaction_history is not compacted
            assert post_compaction_history == compacted

            messages = compacted + [
                {"role": "tool", "content": "tool result"},
                {"role": "assistant", "content": "final answer"},
            ]
            agent._flush_messages_to_session_db(messages, post_compaction_history)

            rows = db.get_messages("original-session")
            assert [row["content"] for row in rows] == [
                "[CONTEXT COMPACTION] summary",
                "recent question",
                "recent answer",
                "tool result",
                "final answer",
            ]

    def test_bounded_recovery_persists_projection_and_archives_canonical_turns(self):
        """A failed semantic summary leaves one durable, resumable projection."""
        from agent.context_compressor import ContextCompressor
        from agent.conversation_compression import (
            conversation_history_after_compression,
            run_automatic_compression,
        )
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "agent.context_compressor.get_model_context_length", return_value=100000
        ):
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = self._make_agent(db)
            agent.compression_in_place = True
            agent.context_compressor = ContextCompressor(
                model="test",
                quiet_mode=True,
                protect_first_n=0,
                protect_last_n=0,
            )
            agent._emit_status = MagicMock()
            agent._ensure_db_session()

            original = [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "recent question"},
                {"role": "assistant", "content": "recent answer"},
                {"role": "user", "content": "current request"},
            ]
            agent._flush_messages_to_session_db(original, [])
            agent._compress_context = MagicMock(return_value=(original, "system"))
            candidates = iter(
                [
                    _prepared(190, label="summary-no-progress"),
                    _prepared(40, label="bounded-recovery"),
                ]
            )

            outcome = run_automatic_compression(
                agent,
                original,
                "system",
                prepared_request=_prepared(190),
                prepare_candidate=lambda *_: next(candidates),
            )

            assert outcome.safe_to_continue is True
            assert outcome.compressed is True
            assert outcome.prepared_request.request_id == "bounded-recovery"
            assert sum(
                agent.context_compressor._is_context_summary_content(
                    message.get("content")
                )
                for message in outcome.messages
            ) == 1
            assert [message["content"] for message in outcome.messages[-3:]] == [
                "recent question",
                "recent answer",
                "current request",
            ]

            resumed = db.get_messages_as_conversation("original-session")
            assert [message["content"] for message in resumed] == [
                message["content"] for message in outcome.messages
            ]
            all_rows = db.get_messages("original-session", include_inactive=True)
            assert any(
                row["content"] == "old question" and row["active"] == 0
                for row in all_rows
            )
            assert any(
                row["content"] == "current request" and row["active"] == 0
                for row in all_rows
            )
            assert sum(
                row["content"] == "current request" and row["active"] == 1
                for row in all_rows
            ) == 1

            baseline = conversation_history_after_compression(
                agent, outcome.messages
            )
            continued = outcome.messages + [
                {"role": "assistant", "content": "final answer"}
            ]
            agent._flush_messages_to_session_db(continued, baseline)
            active = db.get_messages("original-session")
            assert [row["content"] for row in active].count("current request") == 1
            assert [row["content"] for row in active][-1] == "final answer"

    def test_repeated_recovery_replaces_prior_projection_with_one_summary(self):
        from agent.context_compressor import ContextCompressor
        from agent.conversation_compression import (
            conversation_history_after_compression,
            run_automatic_compression,
        )
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir, patch(
            "agent.context_compressor.get_model_context_length", return_value=100000
        ):
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = self._make_agent(db)
            agent.context_compressor = ContextCompressor(
                model="test",
                quiet_mode=True,
                protect_first_n=0,
                protect_last_n=0,
            )
            agent._emit_status = MagicMock()
            agent._ensure_db_session()

            initial = [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "first current"},
            ]
            agent._flush_messages_to_session_db(initial, [])
            agent._compress_context = MagicMock(return_value=(initial, "system"))
            first_candidates = iter(
                [_prepared(190, label="no-progress-1"), _prepared(40, label="recovery-1")]
            )
            first = run_automatic_compression(
                agent,
                initial,
                "system",
                prepared_request=_prepared(190),
                prepare_candidate=lambda *_: next(first_candidates),
            )

            second_input = first.messages + [
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "second current"},
            ]
            agent._flush_messages_to_session_db(
                second_input,
                conversation_history_after_compression(agent, first.messages),
            )
            agent._compress_context = MagicMock(return_value=(second_input, "system"))
            second_candidates = iter(
                [_prepared(190, label="no-progress-2"), _prepared(40, label="recovery-2")]
            )
            second = run_automatic_compression(
                agent,
                second_input,
                "system",
                prepared_request=_prepared(190),
                prepare_candidate=lambda *_: next(second_candidates),
            )

            resumed = db.get_messages_as_conversation("original-session")
            assert [message["content"] for message in resumed] == [
                message["content"] for message in second.messages
            ]
            assert sum(
                agent.context_compressor._is_context_summary_content(
                    message.get("content")
                )
                for message in resumed
            ) == 1
            all_rows = db.get_messages("original-session", include_inactive=True)
            assert any(
                row.get("context_projection") == 1 and row["active"] == 0
                for row in all_rows
            )
            assert any(
                row["content"] == "old question" and row["active"] == 0
                for row in all_rows
            )

    def test_tool_checkpoint_preserves_full_archived_rows_and_resumes_compacted(self):
        """Tool-only checkpoints archive full payloads without deleting history."""
        from agent.conversation_compression import maybe_compact_tool_payloads
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = self._make_agent(db)
            agent._ensure_db_session()
            agent.context_compressor.tail_token_budget = 64

            large_args = '{"command":"run","payload":"' + ("a" * 120000) + '"}'
            large_result = "full-result-" + ("z" * 120000)
            messages = [
                {"role": "user", "content": "start"},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call-1", "type": "function",
                    "function": {"name": "terminal", "arguments": large_args},
                }]},
                {"role": "tool", "tool_call_id": "call-1", "tool_name": "terminal",
                 "content": large_result},
                {"role": "assistant", "content": "consumed"},
                {"role": "user", "content": "recent"},
                {"role": "assistant", "content": "recent answer"},
                {"role": "user", "content": "newest"},
                {"role": "assistant", "content": "newest answer"},
            ]
            agent._flush_messages_to_session_db(messages, [])

            compacted, changed = maybe_compact_tool_payloads(agent, messages)

            assert changed is True
            assert compacted[2]["content"] != large_result
            assert len(compacted[2]["content"].encode("utf-8")) <= len(large_result.encode("utf-8")) * 0.20
            active = db.get_messages("original-session")
            assert active[2]["content"] == compacted[2]["content"]
            all_rows = db.get_messages("original-session", include_inactive=True)
            assert any(row["content"] == large_result for row in all_rows)
            assert any(
                call.get("function", {}).get("arguments") == large_args
                for row in all_rows for call in (row.get("tool_calls") or [])
            )
            assert agent._last_compaction_in_place is True
            assert len(active) == len(messages)
            display = db.get_conversation_page("original-session", limit=20)["messages"]
            assert len(display) == len(messages)
            assert [row["content"] for row in display[:2]] == [
                row["content"] for row in messages[:2]
            ]
            assert [row["content"] for row in display[3:]] == [
                row["content"] for row in messages[3:]
            ]
            assert display[1]["tool_calls"][0]["function"]["arguments"] == large_args
            assert display[2]["content"] == large_result[: db._CONVERSATION_PAGE_MAX_TEXT_CHARS]

    def test_small_tool_checkpoint_archives_once_and_keeps_canonical_history(self):
        """Many consumed sub-512-byte results share one durable checkpoint."""
        from agent.conversation_compression import maybe_compact_tool_payloads
        from hermes_state import SessionDB

        with tempfile.TemporaryDirectory() as tmpdir:
            db = SessionDB(db_path=Path(tmpdir) / "test.db")
            agent = self._make_agent(db)
            agent._ensure_db_session()
            agent.context_compressor.compression_count = 1
            agent.context_compressor.tail_token_budget = 64

            messages = []
            original_results = []
            for index in range(430):
                call_id = f"call-{index}"
                prefix = f'{{"exit_code":0,"item":{index},"data":"'
                result = prefix + ("x" * (192 - len(prefix) - 2)) + '"}'
                original_results.append(result)
                messages.extend([
                    {"role": "user", "content": f"run check {index}"},
                    {"role": "assistant", "content": None, "tool_calls": [{
                        "id": f"response-{index}",
                        "call_id": call_id,
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": '{"command":"true"}',
                        },
                    }]},
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "tool_name": "terminal",
                        "content": result,
                    },
                    {"role": "assistant", "content": f"consumed result {index}"},
                ])
            agent._flush_messages_to_session_db(messages, [])

            compacted, changed = maybe_compact_tool_payloads(agent, messages)

            assert changed is True
            active = db.get_messages("original-session")
            assert len(active) == len(messages)
            assert active[2]["content"] == "[tool result compacted]"
            assert active[-6]["content"] == original_results[-2]
            archived_once = db.get_messages("original-session", include_inactive=True)
            assert any(row["content"] == original_results[0] for row in archived_once)
            display = db.get_display_messages("original-session")
            assert len(display) == len(messages)
            assert display[2]["content"] == original_results[0]
            assert display[1]["tool_calls"][0]["call_id"] == "call-0"

            second, second_changed = maybe_compact_tool_payloads(agent, compacted)

            assert second is compacted
            assert second_changed is False
            assert len(db.get_messages("original-session", include_inactive=True)) == len(archived_once)


# ---------------------------------------------------------------------------
# Part 2: Gateway-side — history_offset after session split
# ---------------------------------------------------------------------------

class TestGatewayHistoryOffsetAfterSplit:
    """Verify that when the agent creates a new session during compression,
    the gateway uses history_offset=0 so all compressed messages are written
    to the JSONL transcript."""

    def test_history_offset_zero_on_session_split(self):
        """When agent.session_id differs from the original, history_offset must be 0."""
        # This tests the logic in gateway/run.py run_sync():
        # _session_was_split = agent.session_id != session_id
        # _effective_history_offset = 0 if _session_was_split else len(agent_history)

        original_session_id = "session-abc"
        agent_session_id = "session-compressed-xyz"  # Different = compression happened
        agent_history_len = 200

        # Simulate the gateway's offset calculation (post-fix)
        _session_was_split = (agent_session_id != original_session_id)
        _effective_history_offset = 0 if _session_was_split else agent_history_len

        assert _session_was_split is True
        assert _effective_history_offset == 0

    def test_history_offset_preserved_without_split(self):
        """When no compression happened, history_offset is the original length."""
        session_id = "session-abc"
        agent_session_id = "session-abc"  # Same = no compression
        agent_history_len = 200

        _session_was_split = (agent_session_id != session_id)
        _effective_history_offset = 0 if _session_was_split else agent_history_len

        assert _session_was_split is False
        assert _effective_history_offset == 200

    def test_new_messages_extraction_after_split(self):
        """After compression with offset=0, new_messages should be ALL agent messages."""
        # Simulates the gateway's new_messages calculation
        agent_messages = [
            {"role": "user", "content": "[CONTEXT COMPACTION] Summary..."},
            {"role": "user", "content": "recent question"},
            {"role": "assistant", "content": "recent answer"},
            {"role": "user", "content": "new question"},
            {"role": "assistant", "content": "new answer"},
        ]
        history_offset = 0  # After fix: 0 on session split

        new_messages = agent_messages[history_offset:] if len(agent_messages) > history_offset else []
        assert len(new_messages) == 5, (
            f"Expected all 5 messages with offset=0, got {len(new_messages)}"
        )

    def test_new_messages_empty_with_stale_offset(self):
        """Demonstrates the bug: stale offset produces empty new_messages."""
        agent_messages = [
            {"role": "user", "content": "summary"},
            {"role": "assistant", "content": "answer"},
        ]
        # Bug: offset is the pre-compression history length
        history_offset = 200

        new_messages = agent_messages[history_offset:] if len(agent_messages) > history_offset else []
        assert len(new_messages) == 0, (
            "Expected 0 messages with stale offset=200 (demonstrates the bug)"
        )
