"""Scheduler provider notification interface.

Execution is owned by authenticated Owner Workers. Providers may reconcile
external schedules, but they do not execute jobs or deliver results.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class CronScheduler(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Short provider identifier."""

    def is_available(self) -> bool:
        return True

    def on_jobs_changed(self) -> None:
        return None

    def reconcile(self) -> None:
        return None


class InProcessCronScheduler(CronScheduler):
    @property
    def name(self) -> str:
        return "owner-worker"


def resolve_cron_scheduler() -> CronScheduler:
    """Return a provider used only for schedule reconciliation notifications."""
    from hermes_cli.config import cfg_get, load_config

    name = ""
    try:
        name = (cfg_get(load_config(), "cron", "provider", default="") or "").strip()
    except Exception:
        pass
    if not name or name in {"builtin", "in-process", "inprocess", "owner-worker"}:
        return InProcessCronScheduler()
    try:
        from plugins.cron_providers import load_cron_scheduler

        provider = load_cron_scheduler(name)
        if provider is not None and provider.is_available():
            return provider
    except Exception:
        pass
    return InProcessCronScheduler()
