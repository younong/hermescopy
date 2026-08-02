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
    def _agent(compressed_messages):
        compressor = SimpleNamespace(
            threshold_tokens=100,
            calibrated_prompt_tokens=lambda tokens: tokens,
            would_hard_block=lambda tokens: tokens >= 190,
        )
        agent = SimpleNamespace(
            context_compressor=compressor,
            tools=[],
            session_id="session-1",
            _compress_context=MagicMock(
                return_value=(compressed_messages, "compressed prompt")
            ),
            _emit_status=MagicMock(),
        )
        return agent

    def test_blocks_until_compressed_request_is_verified_safe(self, monkeypatch):
        original = [{"role": "user", "content": "large history"}]
        compacted = [{"role": "user", "content": "summary"}]
        agent = self._agent(compacted)
        estimates = iter([150, 40])
        monkeypatch.setattr(
            "agent.conversation_compression.estimate_request_tokens_rough",
            lambda *_args, **_kwargs: next(estimates),
        )

        outcome = run_automatic_compression(agent, original, "system")

        assert outcome.safe_to_continue is True
        assert outcome.messages is compacted
        assert outcome.request_tokens == 40
        agent._emit_status.assert_has_calls(
            [
                call("Compressing context (pass 1/3)…", kind="compression.preparing"),
                call("Context compression completed.", kind="compression.completed"),
            ]
        )

    def test_no_progress_below_hard_boundary_degrades_safely(self, monkeypatch):
        original = [{"role": "user", "content": "large history"}]
        agent = self._agent(original)
        monkeypatch.setattr(
            "agent.conversation_compression.estimate_request_tokens_rough",
            lambda *_args, **_kwargs: 150,
        )

        outcome = run_automatic_compression(agent, original, "system")

        assert outcome.safe_to_continue is True
        assert outcome.degraded is True
        assert outcome.messages is original
        assert outcome.compressed is False
        assert outcome.failure_reason == "compression_no_progress"
        agent._emit_status.assert_any_call(
            "Context compression could not reduce the request, but it remains below "
            "the safe context boundary. Continuing with the preserved conversation.",
            kind="compression.degraded",
        )

    def test_no_progress_at_hard_boundary_blocks(self, monkeypatch):
        original = [{"role": "user", "content": "large history"}]
        agent = self._agent(original)
        monkeypatch.setattr(
            "agent.conversation_compression.estimate_request_tokens_rough",
            lambda *_args, **_kwargs: 190,
        )

        outcome = run_automatic_compression(agent, original, "system")

        assert outcome.safe_to_continue is False
        assert outcome.degraded is False
        assert outcome.failure_reason == "compression_no_progress"
        agent._emit_status.assert_any_call(
            "Context compression could not reduce the request safely. The durable "
            "conversation was preserved and no normal model request was sent. Run "
            "/compress to retry or /new to start fresh.",
            kind="compression.blocked",
        )

    def test_partial_progress_below_hard_boundary_degrades_safely(self, monkeypatch):
        original = [
            {"role": "user", "content": "large history"},
            {"role": "assistant", "content": "large response"},
        ]
        compacted = [{"role": "user", "content": "summary"}]
        agent = self._agent(compacted)
        estimates = iter([180, 120])
        monkeypatch.setattr(
            "agent.conversation_compression.estimate_request_tokens_rough",
            lambda *_args, **_kwargs: next(estimates),
        )

        outcome = run_automatic_compression(agent, original, "system")

        assert outcome.safe_to_continue is True
        assert outcome.degraded is True
        assert outcome.compressed is True
        assert outcome.messages is compacted
        assert outcome.failure_reason is None

    def test_exception_below_hard_boundary_degrades_safely(self, monkeypatch):
        original = [{"role": "user", "content": "large history"}]
        agent = self._agent(original)
        agent._compress_context.side_effect = RuntimeError("summary unavailable")
        monkeypatch.setattr(
            "agent.conversation_compression.estimate_request_tokens_rough",
            lambda *_args, **_kwargs: 150,
        )

        outcome = run_automatic_compression(agent, original, "system")

        assert outcome.safe_to_continue is True
        assert outcome.degraded is True
        assert outcome.failure_reason == "compression_failed"

    def test_forced_exception_below_hard_boundary_remains_strict(self, monkeypatch):
        original = [{"role": "user", "content": "large history"}]
        agent = self._agent(original)
        agent._compress_context.side_effect = RuntimeError("summary unavailable")
        monkeypatch.setattr(
            "agent.conversation_compression.estimate_request_tokens_rough",
            lambda *_args, **_kwargs: 150,
        )

        outcome = run_automatic_compression(agent, original, "system", force=True)

        assert outcome.safe_to_continue is False
        assert outcome.degraded is False
        assert outcome.failure_reason == "compression_failed"
