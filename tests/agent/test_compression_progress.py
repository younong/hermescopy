"""Regression: detect compression progress by tokens, not just rows.

Issue #39548: preflight compression in the turn prologue was checking
``len(messages) >= _orig_len`` to decide "Cannot compress further". This
false-positives when a pass summarises message contents — reducing the
estimated request token count without removing any rows — and surfaces a
spurious ``Context length exceeded`` failure followed by an auto-reset of
an otherwise healthy session.

These tests pin the contract of ``_compression_made_progress``: a
row-count reduction OR a *material* (>5%) token-count reduction counts as
progress.
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.prepared_model_request import PreparedModelRequest, PreparedRequestAccounting
from unittest.mock import MagicMock, call

from agent.conversation_compression import (
    _compression_made_progress,
    run_automatic_compression,
)


class TestCompressionMadeProgress:
    def test_rows_reduced_counts_as_progress(self):
        """Removing message rows is the obvious progress signal."""
        assert _compression_made_progress(
            orig_len=10, new_len=5, orig_tokens=1000, new_tokens=1000
        ) is True

    def test_tokens_reduced_without_row_change_counts_as_progress(self):
        """Issue #39548: 220 → 220 rows, 288k → 183k tokens IS progress."""
        assert _compression_made_progress(
            orig_len=220, new_len=220, orig_tokens=288_028, new_tokens=183_180
        ) is True

    def test_both_reduced_counts_as_progress(self):
        """Common case: summarising drops some rows and shrinks the rest."""
        assert _compression_made_progress(
            orig_len=220, new_len=180, orig_tokens=288_028, new_tokens=150_000
        ) is True

    def test_neither_moved_means_no_progress(self):
        """The genuine "stuck" case — same rows, same tokens, give up."""
        assert _compression_made_progress(
            orig_len=10, new_len=10, orig_tokens=1000, new_tokens=1000
        ) is False

    def test_rows_grew_and_tokens_grew_means_no_progress(self):
        """Pathological: the pass made the request larger — definitely stuck."""
        assert _compression_made_progress(
            orig_len=10, new_len=12, orig_tokens=1000, new_tokens=1200
        ) is False

    def test_rows_grew_but_tokens_dropped_is_progress(self):
        """Edge: summary rows may expand the row count while shrinking tokens.

        Token reduction alone is sufficient to keep the loop going.
        """
        assert _compression_made_progress(
            orig_len=10, new_len=11, orig_tokens=1000, new_tokens=600
        ) is True

    def test_tokens_grew_but_rows_dropped_is_progress(self):
        """Edge: row reduction alone is sufficient even if tokens nominally
        creep up (e.g. summary verbosity).  Row-count reduction is a hard
        signal that the transcript actually shrank.
        """
        assert _compression_made_progress(
            orig_len=10, new_len=5, orig_tokens=1000, new_tokens=1100
        ) is True

    def test_sub_5pct_token_drop_is_not_progress(self):
        """A token reduction below the 5% material floor does NOT count as
        progress — matching the overflow-handler retry path (#39550) so a
        marginal wobble can't keep the multi-pass loop spinning."""
        # 1000 -> 970 is a 3% drop, below the 5% floor.
        assert _compression_made_progress(
            orig_len=10, new_len=10, orig_tokens=1000, new_tokens=970
        ) is False
        # 1000 -> 940 is a 6% drop, above the floor.
        assert _compression_made_progress(
            orig_len=10, new_len=10, orig_tokens=1000, new_tokens=940
        ) is True

    def test_zero_orig_tokens_is_not_progress(self):
        """Degenerate estimate (0 tokens) must not be read as a token win."""
        assert _compression_made_progress(
            orig_len=10, new_len=10, orig_tokens=0, new_tokens=0
        ) is False


class TestAutomaticCompression:
    @staticmethod
    def _prepared(tokens, *, threshold=100, hard_limit=190, label="request"):
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
                compression_threshold=threshold,
                hard_input_limit=hard_limit,
                categories=(),
            ),
            message_count=1,
            tool_count=0,
            request_char_count=len(label),
        )

    @staticmethod
    def _agent(compressed_messages, *, recovery_projection=None):
        compressor = SimpleNamespace(
            protect_last_n=0,
            compression_count=0,
            on_session_start=MagicMock(),
        )
        if recovery_projection is not None:
            compressor.build_recovery_projection = MagicMock(
                return_value=recovery_projection
            )
        session_db = MagicMock()
        session_db.try_acquire_compression_lock.return_value = True
        agent = SimpleNamespace(
            context_compressor=compressor,
            session_id="session-1",
            _session_db=session_db,
            _memory_manager=None,
            _flushed_db_message_ids={123},
            _last_flushed_db_idx=4,
            _last_compaction_in_place=False,
            _compress_context=MagicMock(
                return_value=(compressed_messages, "compressed prompt")
            ),
            _emit_status=MagicMock(),
        )
        return agent

    def test_blocks_until_compressed_request_is_verified_safe(self):
        original = [{"role": "user", "content": "large history"}]
        compacted = [{"role": "user", "content": "summary"}]
        agent = self._agent(compacted)

        outcome = run_automatic_compression(
            agent,
            original,
            "system",
            prepared_request=self._prepared(150),
            prepare_candidate=lambda *_: self._prepared(40, label="candidate"),
        )

        assert outcome.safe_to_continue is True
        assert outcome.messages is compacted
        assert outcome.request_tokens == 40
        assert outcome.prepared_request.accounting.effective_input_tokens == 40
        agent._emit_status.assert_has_calls(
            [
                call("Compressing context (pass 1/3)…", kind="compression.preparing"),
                call("Context compression completed.", kind="compression.completed"),
            ]
        )

    def test_preserved_current_turn_tracks_by_identity_after_compression(self):
        old_attached = {
            "role": "user",
            "content": "old",
            "attachments": [{"kind": "image", "name": "old.png"}],
        }
        current = {"role": "user", "content": "current"}
        original = [old_attached, current]
        compacted = [old_attached, {"role": "assistant", "content": "summary"}, current]
        agent = self._agent(compacted)

        outcome = run_automatic_compression(
            agent,
            original,
            "system",
            preserve_attachment_index=1,
            prepared_request=self._prepared(150),
            prepare_candidate=lambda *_: self._prepared(40, label="candidate"),
        )

        assert outcome.safe_to_continue is True
        assert agent._compress_context.call_args.kwargs["preserve_attachment_index"] == 1

    def test_no_progress_below_hard_boundary_degrades_safely(self):
        original = [{"role": "user", "content": "large history"}]
        agent = self._agent(original)
        prepared = self._prepared(150)

        outcome = run_automatic_compression(
            agent,
            original,
            "system",
            prepared_request=prepared,
            prepare_candidate=lambda *_: self._prepared(150, label="candidate"),
        )

        assert outcome.safe_to_continue is True
        assert outcome.degraded is True
        assert outcome.messages is original
        assert outcome.compressed is False
        assert outcome.failure_reason == "compression_no_progress"
        assert outcome.prepared_request is prepared
        agent._emit_status.assert_any_call(
            "Context compression could not reduce the request, but it remains below "
            "the safe context boundary. Continuing with the preserved conversation.",
            kind="compression.degraded",
        )

    def test_no_progress_at_hard_boundary_blocks(self):
        original = [{"role": "user", "content": "large history"}]
        agent = self._agent(original)

        outcome = run_automatic_compression(
            agent,
            original,
            "system",
            prepared_request=self._prepared(190),
            prepare_candidate=lambda *_: self._prepared(190, label="candidate"),
        )

        assert outcome.safe_to_continue is False
        assert outcome.degraded is False
        assert outcome.failure_reason == "compression_no_progress"
        agent._emit_status.assert_any_call(
            "Context compression could not reduce the request safely. The durable "
            "conversation was preserved and no normal model request was sent. Run "
            "/compress to retry or /new to start fresh.",
            kind="compression.blocked",
        )

    def test_hard_block_uses_bounded_recovery_after_summary_no_progress(self):
        original = [{"role": "user", "content": "large history"}]
        recovered = [{"role": "user", "content": "bounded recovery"}]
        agent = self._agent(original, recovery_projection=recovered)
        prepared_calls = iter(
            [
                self._prepared(190, label="compressed-no-progress"),
                self._prepared(40, label="recovery"),
            ]
        )

        outcome = run_automatic_compression(
            agent,
            original,
            "system",
            prepared_request=self._prepared(190),
            prepare_candidate=lambda *_: next(prepared_calls),
        )

        assert outcome.safe_to_continue is True
        assert outcome.degraded is True
        assert outcome.compressed is True
        assert outcome.messages is recovered
        assert outcome.failure_reason == "compression_no_progress"
        assert outcome.prepared_request.request_id == "recovery"
        agent.context_compressor.build_recovery_projection.assert_called_once_with(
            original,
            mode="summary",
            preserve_attachment_index=None,
        )
        agent._session_db.archive_and_compact.assert_called_once_with(
            "session-1", recovered
        )
        assert agent._last_compaction_in_place is True
        assert agent._last_flushed_db_idx == 0
        assert agent._flushed_db_message_ids == set()

    def test_recovery_shrinks_to_minimal_projection_when_summary_still_overflows(self):
        original = [{"role": "user", "content": "large history"}]
        summary_projection = [{"role": "user", "content": "summary recovery"}]
        minimal_projection = [{"role": "user", "content": "minimal recovery"}]
        agent = self._agent(original)
        agent.context_compressor.build_recovery_projection = MagicMock(
            side_effect=[summary_projection, minimal_projection]
        )
        prepared_calls = iter(
            [
                self._prepared(190, label="compressed-no-progress"),
                self._prepared(190, label="summary-recovery"),
                self._prepared(40, label="minimal-recovery"),
            ]
        )

        outcome = run_automatic_compression(
            agent,
            original,
            "system",
            prepared_request=self._prepared(190),
            prepare_candidate=lambda *_: next(prepared_calls),
        )

        assert outcome.safe_to_continue is True
        assert outcome.messages is minimal_projection
        assert outcome.prepared_request.request_id == "minimal-recovery"
        assert [
            item.kwargs["mode"]
            for item in agent.context_compressor.build_recovery_projection.call_args_list
        ] == ["summary", "minimal"]

    def test_recovery_without_durable_store_is_request_local(self):
        original = [{"role": "user", "content": "large history"}]
        recovered = [{"role": "user", "content": "bounded recovery"}]
        agent = self._agent(original, recovery_projection=recovered)
        agent._session_db = None
        prepared_calls = iter(
            [
                self._prepared(190, label="compressed-no-progress"),
                self._prepared(40, label="request-local-recovery"),
            ]
        )

        outcome = run_automatic_compression(
            agent,
            original,
            "system",
            prepared_request=self._prepared(190),
            prepare_candidate=lambda *_: next(prepared_calls),
        )

        assert outcome.safe_to_continue is True
        assert outcome.compressed is False
        assert outcome.messages is original
        assert outcome.prepared_request.request_id == "request-local-recovery"
        assert agent._last_compaction_in_place is False

    def test_recovery_blocks_when_minimum_request_still_overflows(self):
        original = [{"role": "user", "content": "large history"}]
        recovered = [{"role": "user", "content": "minimal recovery"}]
        agent = self._agent(original, recovery_projection=recovered)

        outcome = run_automatic_compression(
            agent,
            original,
            "system",
            prepared_request=self._prepared(190),
            prepare_candidate=lambda *_: self._prepared(190, label="still-large"),
        )

        assert outcome.safe_to_continue is False
        assert outcome.failure_reason == "compression_no_progress"
        agent._session_db.archive_and_compact.assert_not_called()

    def test_partial_progress_below_hard_boundary_degrades_safely(self):
        original = [
            {"role": "user", "content": "large history"},
            {"role": "assistant", "content": "large response"},
        ]
        compacted = [{"role": "user", "content": "summary"}]
        agent = self._agent(compacted)
        candidate = self._prepared(120, label="candidate")

        outcome = run_automatic_compression(
            agent,
            original,
            "system",
            prepared_request=self._prepared(180),
            prepare_candidate=lambda *_: candidate,
        )

        assert outcome.safe_to_continue is True
        assert outcome.degraded is True
        assert outcome.compressed is True
        assert outcome.messages is compacted
        assert outcome.prepared_request is candidate
        assert outcome.failure_reason is None

    def test_exception_below_hard_boundary_degrades_safely(self):
        original = [{"role": "user", "content": "large history"}]
        agent = self._agent(original)
        agent._compress_context.side_effect = RuntimeError("summary unavailable")
        prepared = self._prepared(150)

        outcome = run_automatic_compression(
            agent,
            original,
            "system",
            prepared_request=prepared,
            prepare_candidate=lambda *_: self._prepared(40, label="candidate"),
        )

        assert outcome.safe_to_continue is True
        assert outcome.degraded is True
        assert outcome.failure_reason == "compression_failed"
        assert outcome.prepared_request is prepared

    def test_forced_exception_below_hard_boundary_remains_strict(self):
        original = [{"role": "user", "content": "large history"}]
        agent = self._agent(original)
        agent._compress_context.side_effect = RuntimeError("summary unavailable")

        outcome = run_automatic_compression(
            agent,
            original,
            "system",
            force=True,
            prepared_request=self._prepared(150),
            prepare_candidate=lambda *_: self._prepared(40, label="candidate"),
        )

        assert outcome.safe_to_continue is False
        assert outcome.degraded is False
        assert outcome.failure_reason == "compression_failed"
