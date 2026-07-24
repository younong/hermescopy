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


async def ensure_owner_worker_ready(request: Request) -> tuple[Any, Any]:
    """Return the verified owner context and its ready worker handle."""
    session = getattr(request.state, "session", None)
    if session is None:
        raise HTTPException(status_code=401, detail="Unauthorized")

    owner = owner_context_from_session(session)
    ensure_owner_home(owner)
    supervisor = getattr(request.app.state, "owner_worker_supervisor", None)
    if supervisor is None:
        raise HTTPException(status_code=503, detail="Owner worker supervisor is unavailable")

    try:
        handle = await asyncio.to_thread(supervisor.get_or_start, owner)
    except TimeoutError as exc:
        _log.warning("owner worker startup timed out: %s", exc)
        raise HTTPException(status_code=503, detail="Owner worker startup timed out") from exc
    except (OwnerWorkerUnavailableError, OwnerWorkerStartupError) as exc:
        _log.warning("owner worker unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Owner worker is unavailable") from exc
    except OwnerWorkerHealthError as exc:
        _log.warning("owner worker health check failed: %s", exc)
        raise HTTPException(status_code=502, detail="Owner worker request failed") from exc

    if str(handle.owner_key) != str(owner.owner_key):
        _log.error("owner worker returned a mismatched handle")
        raise HTTPException(status_code=502, detail="Owner worker request failed")
    return owner, handle
