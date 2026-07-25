"""Authenticated Session Reader readiness and warmup boundary."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import HTTPException, Request

from hermes_cli.dashboard_auth.owner_context import ensure_owner_home, owner_context_from_session
from .client import SessionReaderHealthError
from .supervisor import SessionReaderStartupError, SessionReaderUnavailableError

_log = logging.getLogger(__name__)
_TASKS_ATTR = "session_reader_warmup_tasks"
_ACCEPTING_ATTR = "session_reader_warmup_accepting"


def initialize_session_reader_warmups(app: Any) -> None:
    setattr(app.state, _TASKS_ATTR, {})
    setattr(app.state, _ACCEPTING_ATTR, True)


def _state(app: Any) -> tuple[dict[str, asyncio.Task[None]], bool]:
    tasks = getattr(app.state, _TASKS_ATTR, None)
    if tasks is None:
        tasks = {}
        setattr(app.state, _TASKS_ATTR, tasks)
        setattr(app.state, _ACCEPTING_ATTR, True)
    return tasks, bool(getattr(app.state, _ACCEPTING_ATTR, True))


def _start(supervisor: Any, owner: Any) -> Any:
    ensure_owner_home(owner)
    handle = supervisor.get_or_start(owner)
    if str(handle.owner_key) != str(owner.owner_key):
        raise SessionReaderHealthError("session reader returned a mismatched handle")
    return handle


async def start_session_reader(supervisor: Any, owner: Any) -> Any:
    return await asyncio.to_thread(_start, supervisor, owner)


def schedule_session_reader_warmup(
    app: Any, *, owner: Any, supervisor: Any | None = None
) -> asyncio.Task[None] | None:
    tasks, accepting = _state(app)
    if not accepting:
        return None
    supervisor = supervisor or getattr(app.state, "session_reader_supervisor", None)
    if supervisor is None:
        return None
    owner_key = str(owner.owner_key)
    existing = tasks.get(owner_key)
    if existing is not None and not existing.done():
        return existing

    async def warm() -> None:
        try:
            await start_session_reader(supervisor, owner)
        except (TimeoutError, SessionReaderUnavailableError, SessionReaderStartupError, SessionReaderHealthError):
            _log.warning("session reader background warmup failed owner=%s", owner_key)
        except Exception as exc:
            _log.warning(
                "session reader background warmup failed owner=%s error_type=%s",
                owner_key,
                type(exc).__name__,
            )

    task = asyncio.create_task(warm(), name=f"session-reader-warmup:{owner_key}")
    tasks[owner_key] = task

    def discard(completed: asyncio.Task[None]) -> None:
        if tasks.get(owner_key) is completed:
            tasks.pop(owner_key, None)

    task.add_done_callback(discard)
    return task


async def drain_session_reader_warmups(app: Any) -> None:
    tasks, _accepting = _state(app)
    setattr(app.state, _ACCEPTING_ATTR, False)
    if tasks:
        await asyncio.gather(*tuple(tasks.values()), return_exceptions=True)
    tasks.clear()


async def ensure_session_reader_ready(request: Request) -> tuple[Any, Any]:
    session = getattr(request.state, "session", None)
    if session is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    owner = owner_context_from_session(session)
    supervisor = getattr(request.app.state, "session_reader_supervisor", None)
    if supervisor is None:
        raise HTTPException(status_code=503, detail="Session reader supervisor is unavailable")
    try:
        handle = await start_session_reader(supervisor, owner)
    except TimeoutError as exc:
        raise HTTPException(status_code=503, detail="Session reader startup timed out") from exc
    except (SessionReaderUnavailableError, SessionReaderStartupError) as exc:
        raise HTTPException(status_code=503, detail="Session reader is unavailable") from exc
    except SessionReaderHealthError as exc:
        raise HTTPException(status_code=502, detail="Session reader request failed") from exc
    return owner, handle
