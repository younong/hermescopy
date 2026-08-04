"""Scheduler providers cannot own cron execution after Owner Worker migration."""
from __future__ import annotations


def test_builtin_provider_only_exposes_reconciliation_notifications():
    from cron.scheduler_provider import InProcessCronScheduler

    provider = InProcessCronScheduler()
    assert provider.name == "owner-worker"
    assert provider.is_available() is True
    assert provider.on_jobs_changed() is None
    assert provider.reconcile() is None
    assert not hasattr(provider, "start")
    assert not hasattr(provider, "fire_due")


def test_provider_abc_has_no_execution_or_delivery_hooks():
    from cron.scheduler_provider import CronScheduler

    assert "start" not in CronScheduler.__dict__
    assert "fire_due" not in CronScheduler.__dict__
    assert "stop" not in CronScheduler.__dict__


def test_default_resolution_returns_owner_worker_provider(monkeypatch):
    import hermes_cli.config as config
    from cron.scheduler_provider import InProcessCronScheduler, resolve_cron_scheduler

    monkeypatch.setattr(config, "load_config", lambda: {})
    assert isinstance(resolve_cron_scheduler(), InProcessCronScheduler)
