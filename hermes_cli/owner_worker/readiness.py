"""Shared authenticated Owner Worker readiness boundary."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException, Request

from hermes_cli.dashboard_auth.owner_context import (
    ensure_owner_home,
    owner_context_from_session,
)
from hermes_cli.owner_worker import (
    OwnerWorkerHealthError,
    OwnerWorkerStartupError,
    OwnerWorkerUnavailableError,
)


_log = logging.getLogger(__name__)

_WARMUP_TASKS_ATTR = "owner_worker_warmup_tasks"
_WARMUP_ACCEPTING_ATTR = "owner_worker_warmup_accepting"


def initialize_owner_worker_warmups(app: Any) -> None:
    """Initialise application-owned background warmup state."""
    setattr(app.state, _WARMUP_TASKS_ATTR, {})
    setattr(app.state, _WARMUP_ACCEPTING_ATTR, True)


def _warmup_state(app: Any) -> tuple[dict[str, asyncio.Task[None]], bool]:
    tasks = getattr(app.state, _WARMUP_TASKS_ATTR, None)
    if tasks is None:
        tasks = {}
        setattr(app.state, _WARMUP_TASKS_ATTR, tasks)
        setattr(app.state, _WARMUP_ACCEPTING_ATTR, True)
    accepting = bool(getattr(app.state, _WARMUP_ACCEPTING_ATTR, True))
    return tasks, accepting


def _start_owner_worker(supervisor: Any, owner: Any) -> Any:
    ensure_owner_home(owner)
    handle = supervisor.get_or_start(owner)
    if str(handle.owner_key) != str(owner.owner_key):
        raise OwnerWorkerHealthError("owner worker returned a mismatched handle")
    return handle


async def start_owner_worker(supervisor: Any, owner: Any) -> Any:
    """Admit an owner home and return its ready worker without blocking the loop."""
    return await asyncio.to_thread(_start_owner_worker, supervisor, owner)


def schedule_owner_worker_warmup(
    app: Any, *, owner: Any, supervisor: Any | None = None
) -> asyncio.Task[None] | None:
    """Start one retained, best-effort background warmup for an owner."""
    tasks, accepting = _warmup_state(app)
    if not accepting:
        return None
    supervisor = supervisor or getattr(app.state, "owner_worker_supervisor", None)
    if supervisor is None:
        return None

    owner_key = str(owner.owner_key)
    existing = tasks.get(owner_key)
    if existing is not None and not existing.done():
        return existing

    async def warm() -> None:
        try:
            await start_owner_worker(supervisor, owner)
        except TimeoutError:
            _log.warning("owner worker background warmup failed owner=%s reason=startup_timeout", owner_key)
        except (OwnerWorkerUnavailableError, OwnerWorkerStartupError):
            _log.warning("owner worker background warmup failed owner=%s reason=startup_unavailable", owner_key)
        except OwnerWorkerHealthError:
            _log.warning("owner worker background warmup failed owner=%s reason=health_check_failed", owner_key)
        except Exception as exc:  # noqa: BLE001 - warmup must not alter authentication
            _log.warning(
                "owner worker background warmup failed owner=%s reason=unexpected error_type=%s",
                owner_key,
                type(exc).__name__,
            )

    task = asyncio.create_task(warm(), name=f"owner-worker-warmup:{owner_key}")
    tasks[owner_key] = task

    def discard(completed: asyncio.Task[None]) -> None:
        if tasks.get(owner_key) is completed:
            tasks.pop(owner_key, None)

    task.add_done_callback(discard)
    return task


async def drain_owner_worker_warmups(app: Any) -> None:
    """Stop accepting warmups and wait for every submitted startup thread."""
    tasks, _accepting = _warmup_state(app)
    setattr(app.state, _WARMUP_ACCEPTING_ATTR, False)
    if tasks:
        await asyncio.gather(*tuple(tasks.values()), return_exceptions=True)
    tasks.clear()


async def ensure_owner_worker_ready(request: Request) -> tuple[Any, Any]:
    """Return the verified owner context and its ready worker handle."""
    session = getattr(request.state, "session", None)
    if session is None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    owner = owner_context_from_session(session)
    supervisor = getattr(request.app.state, "owner_worker_supervisor", None)
    if supervisor is None:
        raise HTTPException(status_code=503, detail="Owner worker supervisor is unavailable")

    try:
        handle = await start_owner_worker(supervisor, owner)
    except TimeoutError as exc:
        _log.warning("owner worker startup timed out: %s", exc)
        raise HTTPException(status_code=503, detail="Owner worker startup timed out") from exc
    except (OwnerWorkerUnavailableError, OwnerWorkerStartupError) as exc:
        _log.warning("owner worker unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Owner worker is unavailable") from exc
    except OwnerWorkerHealthError as exc:
        _log.warning("owner worker health check failed: %s", exc)
        raise HTTPException(status_code=502, detail="Owner worker request failed") from exc

    return owner, handle
