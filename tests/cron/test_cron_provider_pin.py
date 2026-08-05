"""Cron Agent execution is pinned to the structured Owner Worker gateway."""
from __future__ import annotations

import pytest


def test_scheduler_never_imports_or_constructs_agent_directly(monkeypatch):
    import builtins
    import cron.scheduler as scheduler

    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "run_agent" or name.startswith("run_agent."):
            raise AssertionError("cron scheduler must not import run_agent")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(RuntimeError, match="structured gateway dispatcher"):
        scheduler.run_job(
            {
                "id": "abc123abc123",
                "prompt": "report",
                "provider": "test-provider",
                "model": "test-model",
            }
        )


def test_scheduler_has_no_adapter_or_standalone_delivery_fallback():
    import cron.scheduler as scheduler

    assert not hasattr(scheduler, "_deliver_result")
    assert not hasattr(scheduler, "_resolve_delivery_target")
    assert not hasattr(scheduler, "cron_delivery_targets")
    assert not hasattr(scheduler, "tick")
    assert not hasattr(scheduler, "AIAgent")
