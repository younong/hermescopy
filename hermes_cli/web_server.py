"""
Hermes Agent — Web UI server.

Provides a FastAPI backend serving the Vite/React frontend and REST API
endpoints for managing configuration, environment variables, and sessions.

Usage:
    python -m hermes_cli.main web          # Start on http://127.0.0.1:9119
    python -m hermes_cli.main web --port 8080
"""

from contextlib import asynccontextmanager, contextmanager

import asyncio
import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import mimetypes
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import zipfile

from hermes_cli._subprocess_compat import windows_detach_flags, windows_hide_flags
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from hermes_cli.channel_identity import register_connector_binding_for_owner

import yaml

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hermes_cli import __version__, __release_date__, session_api
from hermes_cli.config import (
    cfg_get,
    DEFAULT_CONFIG,
    OPTIONAL_ENV_VARS,
    clear_model_endpoint_credentials,
    get_config_path,
    get_env_path,
    get_hermes_home,
    load_config,
    load_env,
    read_raw_config,
    save_config,
    save_env_value,
    remove_env_value,
    check_config_version,
    detect_install_method,
    format_docker_update_message,
    recommended_update_command_for_method,
    redact_key,
    write_channel_connector_config_field,
    _deep_merge,
)
from hermes_cli.memory_providers import (
    MemoryProvider,
    ProviderField,
    get_memory_provider,
)
from hermes_cli.latency_trace import (
    clean_latency_trace_id,
    latency_trace_scope,
    log_latency_stage,
)
from gateway.status import (
    derive_gateway_busy,
    derive_gateway_drainable,
    get_running_pid,
    get_runtime_status_running_pid,
    parse_active_agents,
    read_runtime_status,
)
from utils import env_var_enabled

try:
    from fastapi import (
        FastAPI, File, Form, HTTPException, Query, Request, UploadFile,
        WebSocket, WebSocketDisconnect,
    )
    from fastapi.exceptions import RequestValidationError
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel, ConfigDict
except ImportError:
    # First try lazy-installing the dashboard extras. Only the user actually
    # running `hermes dashboard` needs fastapi+uvicorn; lazy install keeps
    # them out of every other install path. After install, re-import.
    try:
        from tools.lazy_deps import ensure as _lazy_ensure
        _lazy_ensure("tool.dashboard", prompt=False)
        from fastapi import (
            FastAPI, File, Form, HTTPException, Query, Request, UploadFile,
            WebSocket, WebSocketDisconnect,
        )
        from fastapi.exceptions import RequestValidationError
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
        from fastapi.staticfiles import StaticFiles
        from pydantic import BaseModel, ConfigDict
    except Exception:
        raise SystemExit(
            "Web UI requires fastapi and uvicorn.\n"
            f"Install with: {sys.executable} -m pip install 'fastapi' 'uvicorn[standard]'"
        )

WEB_DIST = Path(os.environ["HERMES_WEB_DIST"]) if "HERMES_WEB_DIST" in os.environ else Path(__file__).parent / "web_dist"
_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-channel subscriber registry used by /api/pub (PTY-side gateway → dashboard)
# and /api/events (dashboard → browser sidebar).  Keyed by an opaque channel id
# the chat tab generates on mount; entries auto-evict when the last subscriber
# drops AND the publisher has disconnected.
#
# State lives on app.state (not module-level globals) so that asyncio.Lock is
# created on the running event loop during lifespan startup.  A module-level
# asyncio.Lock() binds to whatever loop was active at import time, which breaks
# when the same module is used across TestClient instances or uvicorn reloads.
# ---------------------------------------------------------------------------

def _start_desktop_cron_ticker(stop_event: "threading.Event", interval: int = 60) -> None:
    """Tick the cron scheduler from inside the desktop dashboard backend.

    The scheduler tick loop normally lives in ``hermes gateway run`` — but the
    desktop app spawns a ``hermes dashboard`` backend, not a gateway, so a cron
    a user creates in the app would never fire. We run the resolved cron
    scheduler provider here (no live adapters; delivery falls back to the
    per-platform send path).

    Cross-process safe: the built-in provider's ``cron.scheduler.tick`` takes
    the ``cron/.tick.lock`` file lock, so this never double-fires alongside a
    real gateway on the same HERMES_HOME — whichever process grabs the lock
    first wins the tick.
    """
    from cron.scheduler_provider import resolve_cron_scheduler

    provider = resolve_cron_scheduler()
    _log.info("Desktop cron scheduler started (provider=%s, interval=%ds)", provider.name, interval)
    provider.start(stop_event, interval=interval)


def _warm_gateway_module() -> None:
    try:
        import hermes_cli.gateway  # noqa: F401
    except Exception:
        pass


def _resolve_restart_drain_timeout() -> float:
    try:
        from hermes_cli.gateway import _get_restart_drain_timeout
        return _get_restart_drain_timeout()
    except ImportError:
        from gateway.restart import DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT
        return DEFAULT_GATEWAY_RESTART_DRAIN_TIMEOUT


@asynccontextmanager
async def _lifespan(app: "FastAPI"):
    app.state.event_channels = {}  # dict[str, set]
    app.state.event_lock = asyncio.Lock()
    app.state.authorized_ws_bridges = {}  # scope digest -> {(websocket, epoch)}
    app.state.authorized_ws_bridges_by_worker = {}  # exact worker fence -> {websocket}
    app.state.revoked_ws_bridge_worker_fences = set()  # exact durable fences
    app.state.authorized_ws_bridge_lock = asyncio.Lock()
    app.state.owner_worker_turn_lease_guards = set()
    app.state.authority_change_sequence = 0
    app.state.worker_change_sequence = 0
    app.state.server_event_loop = asyncio.get_running_loop()
    app.state.authority_change_stop = asyncio.Event()
    app.state.authority_change_task = None
    from hermes_cli.owner_worker.readiness import initialize_owner_worker_warmups
    from hermes_cli.session_reader.readiness import initialize_session_reader_warmups

    initialize_owner_worker_warmups(app)
    initialize_session_reader_warmups(app)

    app.state.owner_cron_dispatch_stop = None
    app.state.owner_cron_dispatch_task = None
    app.state.owner_collaboration_dispatch_stop = None
    app.state.owner_collaboration_dispatch_task = None
    owner_supervisor = getattr(app.state, "owner_worker_supervisor", None)

    from hermes_cli.channel_connectors.bootstrap import (
        bootstrap_channel_connectors,
        ilink_status,
    )

    app.state.weixin_ilink_service = None
    app.state.weixin_ilink_runtime = None
    app.state.weixin_ilink_status = None
    connectors = load_config().get("channel_connectors") or {}
    connector_runtime = await bootstrap_channel_connectors(
        connectors,
        auth_required=bool(getattr(app.state, "auth_required", False)),
        supervisor=getattr(app.state, "owner_worker_supervisor", None),
    )
    app.state.channel_connector_runtime = connector_runtime
    app.state.weixin_ilink_runtime = connector_runtime
    app.state.weixin_ilink_status = ilink_status(connector_runtime)
    app.state.weixin_ilink_service = connector_runtime.get("weixin_ilink")

    if getattr(app.state, "auth_required", False) and owner_supervisor is not None:
        from hermes_cli.channel_dispatch import ChannelOutbox
        from hermes_cli.owner_worker.collaboration_dispatcher import (
            run_owner_collaboration_dispatcher,
        )
        from hermes_cli.owner_worker.cron_dispatcher import run_owner_cron_dispatcher

        outbox = ChannelOutbox(connector_runtime.store) if connector_runtime.store is not None else None
        enqueue_delivery = outbox.enqueue_cron_result if outbox is not None else None
        app.state.owner_cron_dispatch_stop = asyncio.Event()
        app.state.owner_cron_dispatch_task = asyncio.create_task(
            run_owner_cron_dispatcher(
                app.state.owner_cron_dispatch_stop,
                owner_supervisor,
                get_hermes_home(),
                enqueue_delivery=enqueue_delivery,
            )
        )
        app.state.owner_collaboration_dispatch_stop = asyncio.Event()
        app.state.owner_collaboration_dispatch_task = asyncio.create_task(
            run_owner_collaboration_dispatcher(
                app.state.owner_collaboration_dispatch_stop,
                owner_supervisor,
                get_hermes_home(),
                enqueue_delivery=(
                    outbox.enqueue_collaboration_origin if outbox is not None else None
                ),
                delivery_status=(
                    outbox.collaboration_delivery_status if outbox is not None else None
                ),
            )
        )

    # Fire hermes_cli.gateway import into a background thread so the event
    # loop is not blocked and HERMES_DASHBOARD_READY fires without delay.
    # On a cold Windows install the module chain triggers .pyc compilation
    # and Defender real-time scans that can stall the event loop for 15-30s.
    # Running in an executor means the cost is paid in a worker thread while
    # the server socket is already open and accepting probes.
    asyncio.get_event_loop().run_in_executor(None, _warm_gateway_module)

    # Desktop-spawned backends (HERMES_DESKTOP=1) fire cron jobs themselves,
    # since the app has no gateway running the scheduler. Server `hermes
    # dashboard` is unaffected — it relies on its own gateway.
    cron_stop: "threading.Event | None" = None
    cron_thread: "threading.Thread | None" = None
    if os.getenv("HERMES_DESKTOP") == "1":
        cron_stop = threading.Event()
        cron_thread = threading.Thread(
            target=_start_desktop_cron_ticker,
            args=(cron_stop,),
            daemon=True,
            name="desktop-cron-ticker",
        )
        cron_thread.start()

    try:
        yield
    finally:
        owner_cron_stop = getattr(app.state, "owner_cron_dispatch_stop", None)
        owner_cron_task = getattr(app.state, "owner_cron_dispatch_task", None)
        owner_collaboration_stop = getattr(
            app.state, "owner_collaboration_dispatch_stop", None
        )
        owner_collaboration_task = getattr(
            app.state, "owner_collaboration_dispatch_task", None
        )
        if owner_cron_stop is not None:
            owner_cron_stop.set()
        if owner_collaboration_stop is not None:
            owner_collaboration_stop.set()
        for dispatch_task in (owner_cron_task, owner_collaboration_task):
            if dispatch_task is None:
                continue
            try:
                await dispatch_task
            except asyncio.CancelledError:
                pass
        connector_runtime = getattr(app.state, "channel_connector_runtime", None)
        if connector_runtime is not None:
            await connector_runtime.close()
        authority_change_stop = getattr(app.state, "authority_change_stop", None)
        authority_change_task = getattr(app.state, "authority_change_task", None)
        if authority_change_stop is not None:
            authority_change_stop.set()
        if authority_change_task is not None:
            authority_change_task.cancel()
            try:
                await authority_change_task
            except asyncio.CancelledError:
                pass
        from hermes_cli.owner_worker.readiness import drain_owner_worker_warmups
        from hermes_cli.session_reader.readiness import drain_session_reader_warmups

        await drain_session_reader_warmups(app)
        reader_supervisor = getattr(app.state, "session_reader_supervisor", None)
        if reader_supervisor is not None:
            try:
                await reader_supervisor.close_clients()
                await asyncio.to_thread(reader_supervisor.shutdown)
            except Exception:
                _log.exception("session reader shutdown cleanup failed")
        await drain_owner_worker_warmups(app)
        await _drain_owner_worker_turn_lease_guards(app)
        supervisor = getattr(app.state, "owner_worker_supervisor", None)
        if supervisor is not None:
            try:
                await asyncio.to_thread(supervisor.shutdown)
            except Exception:
                _log.exception("owner worker shutdown cleanup failed")
            finally:
                resource_manager = getattr(supervisor, "resource_manager", None)
                if resource_manager is not None:
                    try:
                        resource_manager.close()
                    except Exception:
                        _log.exception("owner resource manager close failed")
        if cron_stop is not None:
            cron_stop.set()


def _get_event_state(app: "FastAPI"):
    """Return (event_channels, event_lock) from app.state.

    Lazily initialises the state if the lifespan hasn't run (e.g. when
    TestClient is constructed without a ``with`` block).  The lifespan
    path is preferred because it guarantees the Lock is created on the
    correct event loop, but the lazy path lets existing non-``with``
    TestClient usages keep working.
    """
    try:
        return app.state.event_channels, app.state.event_lock
    except AttributeError:
        app.state.event_channels = {}
        app.state.event_lock = asyncio.Lock()
        return app.state.event_channels, app.state.event_lock


app = FastAPI(title="Hermes Agent", version=__version__, lifespan=_lifespan)

_FEISHU_SECRET_BODY_SUFFIXES = {
    "/channels/feishu",
    "/channels/feishu/credentials",
}


@app.exception_handler(RequestValidationError)
async def _request_validation_error(request: Request, exc: RequestValidationError):
    path = request.url.path.rstrip("/")
    if any(path.endswith(suffix) for suffix in _FEISHU_SECRET_BODY_SUFFIXES):
        return JSONResponse(status_code=422, content={"detail": "invalid_request"})
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


# Memory-provider OAuth connect routes live in the memory layer, not here.
from hermes_cli.memory_oauth import router as _memory_oauth_router  # noqa: E402

app.include_router(_memory_oauth_router)

from hermes_cli.channel_connectors.weixin_ilink.api import router as _ilink_enrollment_router  # noqa: E402
from hermes_cli.api_ingress import router as _api_ingress_router  # noqa: E402

app.include_router(_ilink_enrollment_router)
app.include_router(_api_ingress_router)

# In-browser Chat tab (/chat and /api/ws). Always enabled: the
# desktop app and the dashboard's own Chat tab both drive the agent over the
# `/api/ws` WebSocket, so the embedded-chat surface is an
# unconditional part of the dashboard.  Kept as a module-level constant (rather
# than inlining ``True`` at every gate) so the WS endpoints and the SPA token
# injection share a single, testable seam.
_DASHBOARD_EMBEDDED_CHAT_ENABLED = True

# Simple rate limiter for the reveal endpoint
_reveal_timestamps: List[float] = []
_REVEAL_MAX_PER_WINDOW = 5
_REVEAL_WINDOW_SECONDS = 30

# CORS: restrict to localhost origins only.  The web UI is intended to run
# locally; binding to 0.0.0.0 with allow_origins=["*"] would let any website
# read/modify config and secrets. A reverse-proxied browser remains same-origin
# with its declared public URL, so trusted-proxy mode does not widen CORS.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Endpoints that do NOT require the session token.  Everything else under
# /api/ is gated by the auth middleware below.
#
# This list is defined in ``hermes_cli.dashboard_auth.public_paths`` so the
# OAuth gate middleware can honour the same allowlist — keeping the two
# gates in lockstep avoids drift like the wildcard-subdomain regression
# where ``/api/status`` was public under the legacy gate but 401'd under
# the OAuth gate (breaking the portal's liveness probe).
#
# Keep the upstream list minimal — only truly non-sensitive, read-only
# endpoints belong there.
# ---------------------------------------------------------------------------
from hermes_cli.dashboard_auth.api_availability import (
    authenticated_control_plane_api_allowed,
    authenticated_owner_worker_api_allowed,
    authenticated_plugin_api_allowed,
    authenticated_session_reader_api_allowed,
)
from hermes_cli.dashboard_auth.public_paths import is_public_api_route


def _authenticated_owner_request(request: Request) -> bool:
    """Return True for dashboard-auth requests that must be owner-worker routed."""
    app_obj = getattr(request, "app", None)
    state = getattr(request, "state", None)
    return bool(
        getattr(getattr(app_obj, "state", None), "auth_required", False)
        and getattr(state, "session", None) is not None
    )


_AUTHENTICATED_ALLOWED_PROFILE_VALUES: frozenset[str] = frozenset({"", "current", "default"})


def _authenticated_profile_value_allowed(profile: Optional[str]) -> bool:
    return str(profile or "").strip().lower() in _AUTHENTICATED_ALLOWED_PROFILE_VALUES


def _reject_authenticated_profile_param(profile: Optional[str]) -> None:
    """Authenticated mode must not let frontend profile select legacy homes."""
    if not _authenticated_profile_value_allowed(profile):
        raise HTTPException(
            status_code=400,
            detail="profile selection is not available in authenticated mode",
        )


def _reject_authenticated_profile_query_params(request: Request) -> None:
    """Reject legacy profile and external owner selectors before proxying."""
    getlist = getattr(request.query_params, "getlist", None)
    values = getlist("profile") if callable(getlist) else [request.query_params.get("profile")]
    for value in values:
        if not _authenticated_profile_value_allowed(value):
            raise HTTPException(
                status_code=400,
                detail="profile selection is not available in authenticated mode",
            )
    for key in ("owner", "owner_home", "owner_key"):
        values = getlist(key) if callable(getlist) else [request.query_params.get(key)]
        if any(str(value or "").strip() for value in values):
            raise HTTPException(
                status_code=400,
                detail="owner selection is not available in authenticated mode",
            )


def _owner_worker_query_string(raw_query: str) -> str:
    """Forward query params to the worker after stripping legacy profile hints."""
    pairs = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(str(raw_query or ""), keep_blank_values=True)
        if key not in {"profile", "owner", "owner_home", "owner_key"}
    ]
    return urllib.parse.urlencode(pairs, doseq=True)


def _reject_authenticated_filesystem_api(request: Request) -> None:
    """Authenticated Control Plane must not serve host filesystem APIs."""
    if _authenticated_owner_request(request):
        from hermes_cli.dashboard_auth.audit import (
            AuthorityAuditEvent,
            AuthorityAuditReason,
            audit_authority,
            new_authority_correlation_id,
        )

        audit_authority(
            AuthorityAuditEvent.FILESYSTEM_DENIED,
            correlation_id=new_authority_correlation_id(),
            reason=AuthorityAuditReason.CONTROL_PLANE_FILESYSTEM_FORBIDDEN,
            audience_class="none",
        )
        raise HTTPException(
            status_code=403,
            detail="Filesystem APIs are not available in authenticated mode",
        )


def _reject_authenticated_control_plane_owner_surface(surface: str = "This API") -> None:
    """Fail closed for owner-sensitive Control Plane handlers in auth mode.

    These handlers operate on host paths, legacy profiles, config/env/skills, or
    other process-global state.  In authenticated deployments those surfaces must
    be explicitly routed to an Owner Worker before they are reachable; relying on
    the outer route classifier alone is too fragile because future allowlist/token
    changes could accidentally expose the legacy handler.
    """
    if getattr(app.state, "auth_required", False):
        raise HTTPException(
            status_code=403,
            detail=f"{surface} is not available in authenticated owner mode until routed through the Owner Worker",
        )


_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


class _NoopOwnerWorkerLease:
    def release(self) -> None:
        return None


def _acquire_owner_worker_use(supervisor: Any, handle: Any) -> Any:
    acquire = getattr(supervisor, "acquire_use", None)
    if callable(acquire):
        return acquire(handle)
    return _NoopOwnerWorkerLease()


def _owner_worker_authority_lease(handle: Any):
    """Construct the exact durable fence without trusting a supervisor helper."""
    from hermes_cli.dashboard_auth.authority import OwnerWorkerAuthorityLease, WorkerLeaseState

    return OwnerWorkerAuthorityLease(
        owner_key=str(handle.owner_key),
        worker_generation=int(handle.worker_generation),
        worker_id=str(handle.worker_id),
        state=WorkerLeaseState.ACTIVE,
        lease_version=int(handle.lease_version),
        recovery_generation=int(handle.recovery_generation),
    )


def _release_owner_worker_iter(iterator: Any, lease: Any):
    try:
        yield from iterator
    finally:
        lease.release()


def _session_reader_unavailable_response() -> Response:
    return JSONResponse(
        {
            "error": "session_reader_unavailable",
            "detail": "Session reader is unavailable",
        },
        status_code=503,
        headers={"Retry-After": "1"},
    )


async def _proxy_authenticated_session_reader_http(request: Request) -> Response:
    """Forward authenticated read-only session traffic to the owner Reader."""
    latency_started_at = time.monotonic()
    trace_id = request.headers.get("x-request-id", "")
    log_latency_stage(_log, trace_id=trace_id, surface="session-reader-proxy", stage="request.received")
    if not _authenticated_owner_request(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    from hermes_cli.session_reader import (
        SessionReaderHealthError,
        SessionReaderUnavailableError,
    )
    from hermes_cli.dashboard_auth.owner_context import owner_context_from_session

    _reject_authenticated_profile_query_params(request)
    supervisor = getattr(request.app.state, "session_reader_supervisor", None)
    lifecycle = getattr(request.app.state, "session_reader_lifecycle", None)
    use: Any | None = None
    try:
        if supervisor is None:
            return _session_reader_unavailable_response()
        owner = owner_context_from_session(request.state.session)
        stage_started_at = time.monotonic()
        use = supervisor.acquire_active(owner)
        handle = use.handle
        log_latency_stage(
            _log,
            trace_id=trace_id,
            surface="session-reader-proxy",
            stage="session_reader.acquired",
            started_at=stage_started_at,
        )
        reader_path = request.url.path
        query = _owner_worker_query_string(request.url.query)
        if query:
            reader_path = f"{reader_path}?{query}"
        forwarded_headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() in {"accept", "accept-encoding", "user-agent", "x-request-id"}
        }
        stage_started_at = time.monotonic()
        response = await supervisor.client_for(handle).request(
            request.method,
            reader_path,
            lease=use.lease,
            headers=forwarded_headers,
        )
        if response.status_code == 401:
            raise SessionReaderHealthError("session reader rejected its exact capability")
        log_latency_stage(
            _log,
            trace_id=trace_id,
            surface="session-reader-proxy",
            stage="reader_http.response",
            started_at=stage_started_at,
            outcome="ok" if response.status_code < 500 else "error",
        )
    except HTTPException:
        if use is not None:
            use.release()
        raise
    except SessionReaderUnavailableError:
        if use is not None:
            use.release()
        return _session_reader_unavailable_response()
    except SessionReaderHealthError:
        if use is not None:
            if lifecycle is not None:
                lifecycle.report_request_failure(use.lease, "transport")
            await supervisor.close_client(use.handle)
            use.release()
        return _session_reader_unavailable_response()
    except Exception as exc:
        if use is not None:
            use.release()
        _log.exception("session reader proxy internal failure path=%s", request.url.path)
        raise HTTPException(status_code=500, detail="Internal server error") from exc

    if use is not None:
        use.release()
    log_latency_stage(
        _log,
        trace_id=trace_id,
        surface="session-reader-proxy",
        stage="request.complete",
        started_at=latency_started_at,
        outcome="ok" if response.status_code < 500 else "error",
    )
    response_headers = {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in _HOP_BY_HOP_HEADERS and name.lower() != "content-length"
    }
    return Response(
        content=response.content,
        status_code=response.status_code,
        headers=response_headers,
        media_type=response.headers.get("content-type"),
    )


_OWNER_WORKER_HTTP_SLOW_PATHS = frozenset({
    ("GET", "/api/model/registrations/catalog"),
    ("POST", "/api/model/registrations"),
    ("PUT", "/api/model/registrations"),
})
_OWNER_WORKER_HTTP_SLOW_TIMEOUT = 30.0


async def _proxy_authenticated_owner_http(request: Request) -> Response:
    """Forward an authenticated owner-scoped HTTP request to its Owner Worker."""
    latency_started_at = time.monotonic()
    latency_trace_id = request.headers.get("x-request-id", "")
    log_latency_stage(
        _log,
        trace_id=latency_trace_id,
        surface="owner-http-proxy",
        stage="request.received",
    )
    if not _authenticated_owner_request(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    from hermes_cli.owner_worker import (
        OwnerWorkerClient,
        OwnerWorkerHealthError,
        OwnerWorkerUnavailableError,
    )
    from hermes_cli.owner_worker.readiness import ensure_owner_worker_ready

    _reject_authenticated_profile_query_params(request)
    supervisor = getattr(request.app.state, "owner_worker_supervisor", None)
    lifecycle = getattr(request.app.state, "owner_worker_lifecycle", None)

    lease: Any | None = None
    try:
        with latency_trace_scope(
            _log,
            trace_id=latency_trace_id,
            surface="owner-http-proxy",
        ):
            _owner, handle = await ensure_owner_worker_ready(request)
        lease = _acquire_owner_worker_use(supervisor, handle)
        content = await request.body()
        worker_path = request.url.path
        query = _owner_worker_query_string(request.url.query)
        if query:
            worker_path = f"{worker_path}?{query}"
        forwarded_headers: dict[str, str] = {}
        for name, value in request.headers.items():
            lname = name.lower()
            if lname in _HOP_BY_HOP_HEADERS or lname in {"host", "authorization", "cookie"}:
                continue
            if lname in {"accept", "accept-encoding", "content-type", "user-agent", "x-request-id"}:
                forwarded_headers[name] = value
        stage_started_at = time.monotonic()
        request_timeout = (
            _OWNER_WORKER_HTTP_SLOW_TIMEOUT
            if (request.method, request.url.path) in _OWNER_WORKER_HTTP_SLOW_PATHS
            else 2.0
        )
        response = await asyncio.to_thread(
            OwnerWorkerClient(
                handle.socket_path,
                control_home=getattr(supervisor, "control_home", None),
                timeout=request_timeout,
            ).request,
            request.method,
            worker_path,
            lease=_owner_worker_authority_lease(handle),
            headers=forwarded_headers,
            content=content,
        )
        if response.status_code == 401:
            raise OwnerWorkerHealthError(
                "owner worker rejected its exact capability"
            )
        log_latency_stage(
            _log,
            trace_id=latency_trace_id,
            surface="owner-http-proxy",
            stage="worker_http.response",
            started_at=stage_started_at,
            outcome="ok" if response.status_code < 500 else "error",
        )
    except HTTPException:
        if lease is not None:
            lease.release()
        raise
    except TimeoutError as exc:
        if lease is not None:
            lease.release()
        _log.warning("owner worker startup timed out: %s", exc)
        raise HTTPException(status_code=503, detail="Owner worker startup timed out") from exc
    except OwnerWorkerUnavailableError as exc:
        if lease is not None:
            lease.release()
        _log.warning(
            "owner worker unavailable method=%s path=%s request_id=%s: %s",
            request.method,
            request.url.path,
            request.headers.get("x-request-id", ""),
            exc,
        )
        raise HTTPException(status_code=503, detail="Owner worker is unavailable") from exc
    except OwnerWorkerHealthError as exc:
        if lease is not None:
            if lifecycle is not None:
                lifecycle.report_request_failure(handle, "transport")
            lease.release()
        _log.warning(
            "owner worker proxy transport failed method=%s path=%s request_id=%s: %s",
            request.method,
            request.url.path,
            request.headers.get("x-request-id", ""),
            exc,
        )
        raise HTTPException(status_code=502, detail="Owner worker request failed") from exc
    except Exception as exc:
        if lease is not None:
            lease.release()
        _log.exception(
            "owner worker proxy internal failure method=%s path=%s request_id=%s",
            request.method,
            request.url.path,
            request.headers.get("x-request-id", ""),
        )
        raise HTTPException(status_code=500, detail="Internal server error") from exc

    log_latency_stage(
        _log,
        trace_id=latency_trace_id,
        surface="owner-http-proxy",
        stage="request.complete",
        started_at=latency_started_at,
        outcome="ok" if response.status_code < 500 else "error",
    )
    response_headers = {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in _HOP_BY_HOP_HEADERS and name.lower() not in {"content-length"}
    }
    if response.is_stream_consumed:
        try:
            return Response(
                content=response.content,
                status_code=response.status_code,
                headers=response_headers,
                media_type=response.headers.get("content-type"),
            )
        finally:
            if lease is not None:
                lease.release()
    if lease is None:
        iterator = response.iter_bytes()
    else:
        iterator = _release_owner_worker_iter(response.iter_bytes(), lease)
    return StreamingResponse(
        iterator,
        status_code=response.status_code,
        headers=response_headers,
        media_type=response.headers.get("content-type"),
    )


# Accepted Host header values for loopback binds. DNS rebinding attacks
# point a victim browser at an attacker-controlled hostname (evil.test)
# which resolves to 127.0.0.1 after a TTL flip — bypassing same-origin
# checks because the browser now considers evil.test and our dashboard
# "same origin". Validating the Host header at the app layer rejects any
# request whose Host isn't one we bound for. See GHSA-ppp5-vxwm-4cf7.
_LOOPBACK_HOST_VALUES: frozenset = frozenset({
    "localhost", "127.0.0.1", "::1",
})


def _host_name(host_header: str) -> str:
    """Return a lower-case hostname from a Host header or URL netloc."""
    h = str(host_header or "").strip()
    if not h:
        return ""
    if h.startswith("["):
        close = h.find("]")
        return h[1:close].lower() if close != -1 else h.strip("[]").lower()
    return (h.rsplit(":", 1)[0] if ":" in h else h).lower()


def _is_accepted_host(
    host_header: str,
    bound_host: str,
    *,
    trusted_proxy_host: str = "",
) -> bool:
    """True if the Host targets the bind or declared trusted-proxy origin."""
    host_only = _host_name(host_header)
    if not host_only:
        return False
    if trusted_proxy_host and host_only == _host_name(trusted_proxy_host):
        return True
    if bound_host in {"0.0.0.0", "::"}:
        return True
    bound_lc = bound_host.lower()
    if bound_lc in _LOOPBACK_HOST_VALUES:
        return host_only in _LOOPBACK_HOST_VALUES
    return host_only == bound_lc


@app.middleware("http")
async def host_header_middleware(request: Request, call_next):
    """Reject requests whose Host header doesn't match the bound interface.

    Defends against DNS rebinding: a victim browser on a localhost
    dashboard is tricked into fetching from an attacker hostname that
    TTL-flips to 127.0.0.1. CORS and same-origin checks don't help —
    the browser now treats the attacker origin as same-origin with the
    dashboard. Host-header validation at the app layer catches it.

    See GHSA-ppp5-vxwm-4cf7.
    """
    # Store the bound host on app.state so this middleware can read it —
    # set by start_server() at listen time.
    bound_host = getattr(app.state, "bound_host", None)
    if bound_host:
        host_header = request.headers.get("host", "")
        trusted_proxy_host = getattr(
            request.app.state, "trusted_proxy_public_host", ""
        )
        if not _is_accepted_host(
            host_header,
            bound_host,
            trusted_proxy_host=trusted_proxy_host,
        ):
            return JSONResponse(
                status_code=400,
                content={
                    "detail": (
                        "Invalid Host header. Dashboard requests must use "
                        "the hostname the server was bound to."
                    ),
                },
            )
    return await call_next(request)


def _session_local_account_role(session: Any) -> Optional[str]:
    """Resolve a durable-local role without trusting browser input."""
    if session is None or session.provider != "basic":
        return None
    try:
        from hermes_cli.dashboard_auth import get_provider

        provider = get_provider("basic")
        resolver = getattr(provider, "local_account_for_session", None)
        account = resolver(session) if callable(resolver) else None
    except Exception:
        return None
    return str(account.role) if account is not None else None


def _local_dashboard_account_role(request: Request) -> Optional[str]:
    return _session_local_account_role(getattr(request.state, "session", None))


def _authenticated_owner_control_plane_gate_response(request: Request) -> Optional[JSONResponse]:
    """Return a fail-closed response for authenticated APIs not behind workers."""
    path = request.url.path
    method = request.method
    if (
        path.startswith("/api/plugins/")
        and _local_dashboard_account_role(request) == "member"
    ):
        return JSONResponse(
            status_code=403,
            content={"detail": "Administrator access required"},
        )
    if (
        getattr(request.app.state, "auth_required", False)
        and getattr(request.state, "session", None) is not None
        and path.startswith("/api/")
        and not getattr(request.state, "token_authenticated", False)
        and not authenticated_control_plane_api_allowed(path, method=method)
        and not authenticated_owner_worker_api_allowed(path, method=method)
        and not authenticated_plugin_api_allowed(path, method=method)
        and not authenticated_session_reader_api_allowed(path, method=method)
    ):
        _log.warning(
            "authenticated API denied method=%s path=%s request_id=%s",
            method,
            path,
            request.headers.get("x-request-id", ""),
        )
        return JSONResponse(
            status_code=403,
            content={"detail": "This API is not available until routed through the Owner Worker"},
        )
    return None


@app.middleware("http")
async def _authenticated_owner_control_plane_gate(request: Request, call_next):
    """Fail closed for authenticated APIs that have not moved behind owner workers."""
    response = _authenticated_owner_control_plane_gate_response(request)
    if response is not None:
        return response
    if (
        _authenticated_owner_request(request)
        and authenticated_owner_worker_api_allowed(request.url.path, method=request.method)
    ):
        return await _proxy_authenticated_owner_http(request)
    return await call_next(request)


@app.middleware("http")
async def _plugin_api_runtime_gate(request: Request, call_next):
    """Block requests to disabled plugin API routes at request time.

    :func:`_mount_plugin_api_routes` gates at import time, but if a plugin
    is disabled *after* the dashboard is already running, its FastAPI router
    remains mounted until restart.  This middleware enforces the enabled/
    disabled policy on every request to ``/api/plugins/{name}/...`` so that
    runtime config changes take effect immediately.

    Registered BEFORE the auth middlewares (so it executes AFTER them): a
    request that hasn't cleared auth must get auth's 401 first, never this
    gate's 404 — otherwise an unauthenticated caller could fingerprint which
    plugins are installed/enabled by reading the status code. We only reach
    the enabled/disabled check for a request that auth already let through.
    """
    path = request.url.path
    if path.startswith("/api/plugins/"):
        # Only gate authenticated requests. Unauthenticated ones fall
        # through so auth_middleware / the OAuth gate return 401 first and
        # this route can't be used as a plugin-name oracle.
        _authed = (
            getattr(request.state, "token_authenticated", False)
            or getattr(request.app.state, "auth_required", False)
        )
        if _authed:
            # Extract plugin name from /api/plugins/<name>/...
            parts = path.split("/")
            # parts: ['', 'api', 'plugins', '<name>', ...]
            if len(parts) >= 4:
                plugin_name = parts[3]
                if plugin_name:
                    try:
                        from hermes_cli.plugins_cmd import (
                            _get_enabled_set,
                            _get_disabled_set,
                        )
                        enabled_set = _get_enabled_set()
                        disabled_set = _get_disabled_set()
                    except Exception:
                        enabled_set = set()
                        disabled_set = set()
                    # Determine plugin source.  Check the cached plugin list;
                    # if not found, assume user plugin (safe default — blocks).
                    plugins = _get_dashboard_plugins()
                    plugin = next(
                        (p for p in plugins if p.get("name") == plugin_name),
                        None,
                    )
                    source = plugin.get("source") if plugin else "user"
                    if source == "user":
                        if plugin_name in disabled_set or plugin_name not in enabled_set:
                            return JSONResponse(
                                status_code=404,
                                content={"detail": "Plugin not found"},
                            )
                    elif source == "bundled":
                        if plugin_name in disabled_set:
                            return JSONResponse(
                                status_code=404,
                                content={"detail": "Plugin not found"},
                            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Dashboard authentication gate. The retained Web surface always requires a
# verified cookie session; the separate machine-token seam owns opted-in API
# routes used by service callers.
# ---------------------------------------------------------------------------


@app.middleware("http")
async def _dashboard_auth_gate(request: Request, call_next):
    from hermes_cli.dashboard_auth.middleware import gated_auth_middleware
    return await gated_auth_middleware(request, call_next)


@app.middleware("http")
async def _token_auth_seam(request: Request, call_next):
    """Outermost auth seam: non-interactive bearer-token auth for opted-in routes.

    Registered LAST so it runs FIRST (Starlette middleware is outermost-last).
    A registered token route is fully owned here — authenticate by token,
    attach the principal + ``token_authenticated`` flag, and let the downstream
    cookie/session gates skip enforcement. Non-token routes pass straight
    through untouched.
    """
    from hermes_cli.dashboard_auth.token_auth import token_auth_middleware
    return await token_auth_middleware(request, call_next)


# ---------------------------------------------------------------------------
# Config schema — auto-generated from DEFAULT_CONFIG
# ---------------------------------------------------------------------------

# Manual overrides for fields that need select options or custom types
_SCHEMA_OVERRIDES: Dict[str, Dict[str, Any]] = {
    "model": {
        "type": "string",
        "description": "Default model (e.g. anthropic/claude-sonnet-4.6)",
        "category": "general",
    },
    "model_context_length": {
        "type": "number",
        "description": "Context window override (0 = auto-detect from model metadata)",
        "category": "general",
    },
    "terminal.backend": {
        "type": "select",
        "description": "Terminal execution backend",
        "options": ["local", "docker", "ssh", "modal", "daytona", "singularity"],
    },
    "terminal.modal_mode": {
        "type": "select",
        "description": "Modal sandbox mode",
        "options": ["sandbox", "function"],
    },
    "tts.provider": {
        "type": "select",
        "description": "Text-to-speech provider",
        "options": ["edge", "elevenlabs", "openai", "neutts"],
    },
    "stt.provider": {
        "type": "select",
        "description": "Speech-to-text provider",
        # "mistral" temporarily removed — mistralai PyPI package quarantined
        # (malicious 2.4.6 release on 2026-05-12). Restore once available.
        "options": ["local", "groq", "openai", "xai", "elevenlabs"],
    },
    "stt.elevenlabs.model_id": {
        "type": "select",
        "description": "ElevenLabs Scribe model",
        "options": ["scribe_v2", "scribe_v1"],
    },
    "display.skin": {
        "type": "select",
        "description": "CLI visual theme",
        "options": ["default", "ares", "mono", "slate"],
    },
    "dashboard.theme": {
        "type": "select",
        "description": "Web dashboard visual theme",
        "options": ["default", "midnight", "ember", "mono", "cyberpunk", "rose"],
    },
    "display.resume_display": {
        "type": "select",
        "description": "How resumed sessions display history",
        "options": ["minimal", "full", "off"],
    },
    "display.busy_input_mode": {
        "type": "select",
        "description": "Input behavior while agent is running",
        "options": ["interrupt", "queue", "steer"],
    },
    "memory.provider": {
        "type": "select",
        "description": "Memory provider plugin",
        "options": ["builtin", "honcho"],
    },
    "approvals.mode": {
        "type": "select",
        "description": "Dangerous command approval mode",
        "options": ["ask", "yolo", "deny"],
    },
    "context.engine": {
        "type": "select",
        "description": "Context management engine",
        "options": ["default", "custom"],
    },
    "human_delay.mode": {
        "type": "select",
        "description": "Simulated typing delay mode",
        "options": ["off", "typing", "fixed"],
    },
    "logging.level": {
        "type": "select",
        "description": "Log level for agent.log",
        "options": ["DEBUG", "INFO", "WARNING", "ERROR"],
    },
    "agent.service_tier": {
        "type": "select",
        "description": "API service tier (OpenAI/Anthropic)",
        "options": ["", "auto", "default", "flex"],
    },
    "delegation.reasoning_effort": {
        "type": "select",
        "description": "Reasoning effort for delegated subagents",
        "options": ["", "low", "medium", "high"],
    },
    "updates.non_interactive_local_changes": {
        "type": "select",
        "description": (
            "When the chat app / gateway updates Hermes (no terminal prompt), "
            "what to do with uncommitted local source edits. 'stash' keeps them "
            "and re-applies them after the update; 'discard' throws them away. "
            "Terminal updates always ask, regardless of this setting."
        ),
        "options": ["stash", "discard"],
    },
}

# Categories with fewer fields get merged into "general" to avoid tab sprawl.
_CATEGORY_MERGE: Dict[str, str] = {
    "privacy": "security",
    "context": "agent",
    "skills": "agent",
    "cron": "agent",
    "network": "agent",
    "checkpoints": "agent",
    "approvals": "security",
    "human_delay": "display",
    "dashboard": "display",
    "code_execution": "agent",
    "prompt_caching": "agent",
    "goals": "agent",
    "updates": "general",
    # `onboarding.profile_build` is the only schema-surfaced onboarding field
    # (`onboarding.seen` is an internal latch dict, not a user setting), so fold
    # it into the agent tab rather than spawning a one-field orphan category.
    "onboarding": "agent",
    # `computer_use.cua_telemetry` is the only schema-surfaced computer_use
    # field — fold it into the agent tab rather than spawning a one-field
    # orphan category.
    "computer_use": "agent",
}

# Display order for tabs — unlisted categories sort alphabetically after these.
_CATEGORY_ORDER = [
    "general", "agent", "terminal", "display", "delegation",
    "memory", "compression", "security", "browser", "voice",
    "tts", "stt", "logging", "discord", "auxiliary",
]


def _infer_type(value: Any) -> str:
    """Infer a UI field type from a Python value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "number"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "string"


def _build_schema_from_config(
    config: Dict[str, Any],
    prefix: str = "",
) -> Dict[str, Dict[str, Any]]:
    """Walk DEFAULT_CONFIG and produce a flat dot-path → field schema dict."""
    schema: Dict[str, Dict[str, Any]] = {}
    for key, value in config.items():
        full_key = f"{prefix}.{key}" if prefix else key

        # Skip internal / version keys
        if full_key in {"_config_version",}:
            continue

        # Category is the first path component for nested keys, or "general"
        # for top-level scalar fields (model, toolsets, timezone, etc.).
        if prefix:
            category = prefix.split(".")[0]
        elif isinstance(value, dict):
            category = key
        else:
            category = "general"

        if isinstance(value, dict):
            # Recurse into nested dicts
            schema.update(_build_schema_from_config(value, full_key))
        else:
            entry: Dict[str, Any] = {
                "type": _infer_type(value),
                "description": full_key.replace(".", " → ").replace("_", " ").title(),
                "category": category,
            }
            # Apply manual overrides
            if full_key in _SCHEMA_OVERRIDES:
                entry.update(_SCHEMA_OVERRIDES[full_key])
            # Merge small categories
            entry["category"] = _CATEGORY_MERGE.get(entry["category"], entry["category"])
            schema[full_key] = entry
    return schema


CONFIG_SCHEMA = _build_schema_from_config(DEFAULT_CONFIG)

# Inject virtual fields that don't live in DEFAULT_CONFIG but are surfaced
# by the normalize/denormalize cycle.  Insert model_context_length right after
# the "model" key so it renders adjacent in the frontend.
_mcl_entry = _SCHEMA_OVERRIDES["model_context_length"]
_ordered_schema: Dict[str, Dict[str, Any]] = {}
for _k, _v in CONFIG_SCHEMA.items():
    _ordered_schema[_k] = _v
    if _k == "model":
        _ordered_schema["model_context_length"] = _mcl_entry
CONFIG_SCHEMA = _ordered_schema


class ConfigUpdate(BaseModel):
    config: dict
    profile: Optional[str] = None


class EnvVarUpdate(BaseModel):
    key: str
    value: str
    profile: Optional[str] = None
    # Optional bearer key for the connectivity probe of a custom/local endpoint
    # (``key == "OPENAI_BASE_URL"``). Self-hosted endpoints that gate
    # ``/v1/models`` behind auth otherwise look "reachable but empty"; sending
    # the key lets the probe enumerate the served models. Ignored for the
    # regular PUT /api/env path (which only reads key/value).
    api_key: str = ""


class EnvVarDelete(BaseModel):
    key: str
    profile: Optional[str] = None


class EnvVarReveal(BaseModel):
    key: str
    profile: Optional[str] = None


class MemoryProviderConfigUpdate(BaseModel):
    values: Dict[str, str] = {}


class MessagingPlatformUpdate(BaseModel):
    enabled: Optional[bool] = None
    env: Dict[str, str] = {}
    clear_env: List[str] = []
    # Explicit body profile beats the query param injected by the global
    # dashboard profile switcher (same precedence as other scoped writes).
    profile: Optional[str] = None


class EmployeeCreate(BaseModel):
    profile: Dict[str, Any]
    activate: bool = True


class EmployeeProfileUpdate(BaseModel):
    expected_revision: int
    profile: Dict[str, Any]


class BuiltinAssistantPersonalizationUpdate(BaseModel):
    expected_revision: int
    nickname: str
    personal_preference: str = ""

    model_config = ConfigDict(extra="forbid")


class EmployeeCollaborationPolicyUpdate(BaseModel):
    may_participate: bool
    may_create_groups: bool
    invite_quota: Optional[int] = None


class EmployeeLifecycleUpdate(BaseModel):
    status: str


class FeishuBindingCreate(BaseModel):
    app_id: str
    app_secret: str
    domain: str = "feishu"
    encrypt_key: str = ""
    verification_token: str = ""
    activate: bool = True


class FeishuBindingCredentialRotate(BaseModel):
    expected_credential_version: int
    app_secret: str
    encrypt_key: Optional[str] = None
    verification_token: Optional[str] = None


class FeishuBindingLifecycleUpdate(BaseModel):
    status: str


class AudioTranscriptionRequest(BaseModel):
    data_url: str
    mime_type: Optional[str] = None


class ManagedFileUpload(BaseModel):
    path: str
    data_url: str
    overwrite: bool = True


class ManagedDirectoryCreate(BaseModel):
    path: str


class ManagedFileDelete(BaseModel):
    path: str
    recursive: bool = False


_AUDIO_MIME_EXTENSIONS: Dict[str, str] = {
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/m4a": ".m4a",
    "audio/mp3": ".mp3",
    "audio/mp4": ".mp4",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/wave": ".wav",
    "audio/webm": ".webm",
    "audio/x-m4a": ".m4a",
    "audio/x-wav": ".wav",
    "video/webm": ".webm",
}
_MAX_TRANSCRIPTION_UPLOAD_BYTES = 25 * 1024 * 1024


def _audio_extension_for_mime(mime_type: str) -> str:
    normalized = (mime_type or "").split(";", 1)[0].strip().lower()
    return _AUDIO_MIME_EXTENSIONS.get(normalized, ".webm")


class ModelRegistrationPayload(BaseModel):
    id: str = ""
    name: str
    kind: str
    provider: str = ""
    model: str
    # None lets model_registrations apply its per-kind default (catalog for
    # chat/image/video, manual for voice/vector).
    source: Optional[str] = None
    base_url: str = ""
    api_mode: str = "openai"
    api_key: str = ""
    context_length: Optional[int] = None
    use_gateway: bool = False
    profile: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class ModelRegistrationMutation(BaseModel):
    id: str
    profile: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class ModelAssignment(BaseModel):
    """Payload for POST /api/model/set — assign a provider/model to a slot.

    scope="main"        → writes model.provider + model.default
    scope="auxiliary"   → writes auxiliary.<task>.provider + auxiliary.<task>.model
    scope="auxiliary" with task=""  → applied to every auxiliary.* slot
    scope="auxiliary" with task="__reset__"  → resets every slot to provider="auto"
    """
    scope: str
    provider: str
    model: str
    task: str = ""
    # Optional OpenAI-compatible endpoint URL. Only honored for custom/local
    # providers on the main slot — lets the GUI configure a self-hosted endpoint
    # (vLLM, llama.cpp, Ollama, …) that needs no API key. The runtime resolver
    # reads model.base_url from config (it ignores OPENAI_BASE_URL), so this is
    # the path that actually wires a local endpoint into resolution.
    base_url: str = ""
    # Optional API key for a custom/local endpoint. Persisted to
    # ``model.api_key`` (where the runtime resolver reads it) so a self-hosted
    # endpoint that requires auth works from the GUI — mirrors the key the
    # ``hermes model`` custom flow collects. Honored only on the main slot for
    # custom/local providers.
    api_key: str = ""
    confirm_expensive_model: bool = False
    profile: Optional[str] = None


class MoaModelSlot(BaseModel):
    provider: str = ""
    model: str = ""


class MoaPresetPayload(BaseModel):
    reference_models: list[MoaModelSlot] = []
    aggregator: MoaModelSlot = MoaModelSlot()
    reference_temperature: float = 0.6
    aggregator_temperature: float = 0.4
    max_tokens: int = 4096
    enabled: bool = True


class MoaConfigPayload(BaseModel):
    default_preset: str = "default"
    active_preset: str = ""
    presets: dict[str, MoaPresetPayload] = {}
    # Backward-compatible flat payload fields used by older dashboard/desktop
    # clients during this PR's transition window.
    reference_models: list[MoaModelSlot] = []
    aggregator: MoaModelSlot = MoaModelSlot()
    reference_temperature: float = 0.6
    aggregator_temperature: float = 0.4
    max_tokens: int = 4096
    enabled: bool = True
    profile: Optional[str] = None


def _normalize_main_model_assignment(provider: str, model: str) -> tuple[str, str]:
    """Normalize a main-slot (provider, model) pair before persisting.

    The Models page has two assignment paths and only one of them was safe:

    - The "Change" picker sends a real Hermes provider slug — fine.
    - The per-card "Use as → Main model" menu sends ``entry.provider``
      from the analytics rows, falling back to the model's VENDOR prefix
      (``modelVendor("anthropic/claude-opus-4.6") == "anthropic"``) when
      the session row has no ``billing_provider`` (older sessions, NULL
      rows).  That wrote ``provider: anthropic`` +
      ``default: anthropic/claude-opus-4.6`` to config — a vendor-prefixed
      OpenRouter slug on the NATIVE Anthropic provider.  New sessions then
      400 against api.anthropic.com ("model: anthropic/claude-opus-4.6 not
      found") and the user reads it as "changing models does nothing".

    Two repairs, both at this single chokepoint so every caller inherits:

    1. Vendor-name → Hermes-provider mapping: when the provider string is
       not a known Hermes provider/alias (e.g. ``moonshotai``, ``x-ai`` is
       known but ``poolside`` isn't) but the model is a vendor-prefixed
       aggregator slug, keep the user's CURRENT aggregator if they're on
       one, else fall back to openrouter.
    2. Model-format normalization for the resolved provider via
       ``normalize_model_for_provider`` (e.g. ``anthropic/claude-opus-4.6``
       on native anthropic → ``claude-opus-4-6``).
    """
    from hermes_cli.models import _KNOWN_PROVIDER_NAMES, normalize_provider
    from hermes_cli.model_normalize import normalize_model_for_provider

    prov_in = (provider or "").strip()
    model_in = (model or "").strip()
    canonical = normalize_provider(prov_in)

    if canonical not in _KNOWN_PROVIDER_NAMES and "/" in model_in:
        # Vendor prefix posing as a provider (analytics fallback). Resolve
        # against the user's current provider when it's an aggregator that
        # serves vendor-prefixed slugs; otherwise default to openrouter.
        try:
            cur_cfg = load_config().get("model", {})
            cur_provider = (
                str(cur_cfg.get("provider", "") or "").strip().lower()
                if isinstance(cur_cfg, dict) else ""
            )
        except Exception:
            cur_provider = ""
        from hermes_cli.models import _AGGREGATOR_PROVIDERS
        if cur_provider and normalize_provider(cur_provider) in _AGGREGATOR_PROVIDERS:
            canonical = normalize_provider(cur_provider)
            prov_in = cur_provider
        else:
            canonical = "openrouter"
            prov_in = "openrouter"

    # Custom/user-config providers keep the model verbatim — the registry
    # normalizer doesn't know their namespaces.
    if canonical in _KNOWN_PROVIDER_NAMES and not canonical.startswith("custom"):
        try:
            normalized_model = normalize_model_for_provider(model_in, canonical)
            if normalized_model:
                model_in = normalized_model
        except Exception:
            _log.debug("model normalization failed for %s/%s", prov_in, model_in, exc_info=True)

    return prov_in, model_in


def _apply_main_model_assignment(
    model_cfg: "Any", provider: str, model: str, base_url: str = "", api_key: str = ""
) -> dict:
    """Apply a main-slot model assignment to a ``model`` config dict in place.

    Sets ``provider``/``default``, then reconciles ``base_url``:

    - An explicitly supplied ``base_url`` is always persisted (covers
      ``custom``/local endpoints and any provider whose key is bound to a
      non-default host).
    - Otherwise, a stale ``base_url`` is cleared ONLY when switching to a
      *different* provider — that URL belonged to the old provider. When the
      provider is unchanged and no new URL is supplied, the existing
      ``base_url`` is preserved. This keeps a user's custom endpoint (e.g. a
      Xiaomi MiMo Token Plan host, ``https://token-plan-*.xiaomimimo.com/v1``)
      alive when they merely re-pick a model under the same provider — picking
      a model previously wiped it, forcing the registry default and breaking
      Token Plan keys.

    The runtime resolver reads ``model.base_url`` from config (it ignores
    ``OPENAI_BASE_URL``) and only honors it when the configured provider matches
    and the pool entry is on the registry default, so preserving it here is what
    lets the override actually route. The hardcoded ``context_length`` override
    is always dropped since the new model may have a different context window.

    Returns the same dict (coerced to a fresh dict if the input wasn't one) so
    callers can assign it straight back onto the model config.
    """
    if not isinstance(model_cfg, dict):
        model_cfg = {}
    prev_provider = str(model_cfg.get("provider") or "").strip().lower()
    new_provider = provider.strip().lower()
    model_cfg["provider"] = provider
    model_cfg["default"] = model
    if base_url.strip():
        model_cfg["base_url"] = base_url.strip()
    elif model_cfg.get("base_url") and new_provider != prev_provider:
        # Switching providers: the old URL belonged to the old provider, drop
        # it so the new provider's default endpoint is used. Same-provider
        # re-assignment keeps the user's configured base_url intact.
        model_cfg["base_url"] = ""
    # The endpoint key follows the same lifecycle as base_url: an explicit key
    # is always persisted; an existing key is dropped only when switching to a
    # different provider (it belonged to the old endpoint), and preserved on a
    # same-provider re-pick so re-selecting a model doesn't wipe the key.
    if api_key.strip():
        model_cfg["api_key"] = api_key.strip()
        model_cfg.pop("api", None)
    elif model_cfg.get("api_key") and new_provider != prev_provider:
        clear_model_endpoint_credentials(model_cfg, clear_api_mode=False)
    if new_provider != prev_provider:
        clear_model_endpoint_credentials(model_cfg, clear_api_key=False)
    model_cfg.pop("context_length", None)
    return model_cfg


_GATEWAY_HEALTH_URL = os.getenv("GATEWAY_HEALTH_URL")
try:
    _GATEWAY_HEALTH_TIMEOUT = float(os.getenv("GATEWAY_HEALTH_TIMEOUT", "3"))
except (ValueError, TypeError):
    _log.warning(
        "Invalid GATEWAY_HEALTH_TIMEOUT value %r — using default 3.0s",
        os.getenv("GATEWAY_HEALTH_TIMEOUT"),
    )
    _GATEWAY_HEALTH_TIMEOUT = 3.0

# DEPRECATED (scheduled for removal): GATEWAY_HEALTH_URL / GATEWAY_HEALTH_TIMEOUT.
# Cross-container / cross-host gateway liveness detection will be folded into a
# first-class dashboard config key so it's no longer Docker-adjacent lore buried
# in env vars.  The env vars still work for now so existing Compose deployments
# don't break.  Do not add new callers — wire new uses through the planned
# config surface.


def _probe_gateway_health() -> tuple[bool, dict | None]:
    """Probe the gateway via its HTTP health endpoint (cross-container).

    .. deprecated::
        Driven by the deprecated ``GATEWAY_HEALTH_URL`` /
        ``GATEWAY_HEALTH_TIMEOUT`` env vars.  Scheduled for removal alongside
        a move to a first-class dashboard config key.  See
        :data:`_GATEWAY_HEALTH_URL` for context.

    Uses ``/health/detailed`` first (returns full state), falling back to
    the simpler ``/health`` endpoint.  Returns ``(is_alive, body_dict)``.

    Accepts any of these as ``GATEWAY_HEALTH_URL``:
    - ``http://gateway:8642``                (base URL — recommended)
    - ``http://gateway:8642/health``         (explicit health path)
    - ``http://gateway:8642/health/detailed`` (explicit detailed path)

    This is a **blocking** call — run via ``run_in_executor`` from async code.
    """
    if not _GATEWAY_HEALTH_URL:
        return False, None

    # Normalise to base URL so we always probe the right paths regardless of
    # whether the user included /health or /health/detailed in the env var.
    base = _GATEWAY_HEALTH_URL.rstrip("/")
    if base.endswith("/health/detailed"):
        base = base[: -len("/health/detailed")]
    elif base.endswith("/health"):
        base = base[: -len("/health")]

    for path in (f"{base}/health/detailed", f"{base}/health"):
        try:
            req = urllib.request.Request(path, method="GET")
            with urllib.request.urlopen(req, timeout=_GATEWAY_HEALTH_TIMEOUT) as resp:
                if resp.status == 200:
                    body = json.loads(resp.read())
                    return True, body
        except Exception:
            continue
    return False, None


# Image MIME types this endpoint will serve. Extension-allowlisted so an
# authenticated caller can't pull non-image files through it.
_MEDIA_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
}
_MEDIA_MAX_BYTES = 25 * 1024 * 1024
_MANAGED_FILES_ROOT_ENV = "HERMES_DASHBOARD_FILES_ROOT"
_MANAGED_FILE_MAX_BYTES = 100 * 1024 * 1024
_HOSTED_MANAGED_FILES_ROOT = Path("/opt/data")


@dataclass(frozen=True)
class ManagedFilesPolicy:
    default_path: Path
    locked_root: Path | None
    can_change_path: bool


_FS_READDIR_HIDDEN = {
    ".git",
    ".hg",
    ".svn",
    ".cache",
    ".next",
    ".turbo",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "venv",
}
_FS_DATA_URL_MAX_BYTES = 16 * 1024 * 1024
_FS_TEXT_SOURCE_MAX_BYTES = 64 * 1024 * 1024
_FS_TEXT_PREVIEW_MAX_BYTES = 512 * 1024
# Upper bound for the in-app spot editor's save. The editor only opens
# non-truncated text (<= the preview cap), so this is a safety ceiling against
# a pasted-in megablob, not the expected payload size.
_FS_TEXT_WRITE_MAX_BYTES = 8 * 1024 * 1024
_FS_PREVIEW_LANGUAGE_BY_EXT = {
    ".c": "c",
    ".conf": "ini",
    ".cpp": "cpp",
    ".css": "css",
    ".csv": "csv",
    ".go": "go",
    ".graphql": "graphql",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "jsx",
    ".kt": "kotlin",
    ".lua": "lua",
    ".md": "markdown",
    ".mjs": "javascript",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".sh": "shell",
    ".sql": "sql",
    ".svg": "xml",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".txt": "text",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".zsh": "shell",
}
_FS_MIME_TYPES = {
    ".avi": "video/x-msvideo",
    ".bmp": "image/bmp",
    ".flac": "audio/flac",
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".m4a": "audio/mp4",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".mp3": "audio/mpeg",
    ".mp4": "video/mp4",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg; codecs=opus",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".wav": "audio/wav",
    ".webm": "video/webm",
    ".webp": "image/webp",
}


def _fs_path(raw_path: str, cwd: str | None = None) -> Path:
    raw = str(raw_path or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Path is required")
    if "\0" in raw:
        raise HTTPException(status_code=400, detail="Invalid path")
    try:
        if raw.lower().startswith("file:"):
            parsed = urllib.parse.urlparse(raw)
            if parsed.netloc and parsed.netloc not in {"", "localhost"}:
                raise ValueError
            raw = urllib.request.url2pathname(parsed.path)
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            base = Path.cwd()
            if cwd is not None:
                raw_cwd = str(cwd or "").strip()
                if not raw_cwd or "\0" in raw_cwd:
                    raise ValueError
                base = Path(raw_cwd).expanduser().resolve(strict=False)
            candidate = base / candidate
        return candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid path")


def _fs_mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in _FS_MIME_TYPES:
        return _FS_MIME_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


def _fs_looks_binary(data: bytes) -> bool:
    if not data:
        return False
    if b"\0" in data:
        return True
    suspicious = sum(1 for byte in data if byte < 32 and byte not in {9, 10, 13})
    return suspicious / len(data) > 0.12


def _fs_regular_file(path: Path) -> tuple[Path, os.stat_result]:
    target = _fs_path(str(path))
    try:
        st = target.stat()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")
    except NotADirectoryError:
        raise HTTPException(status_code=404, detail="File not found")
    except PermissionError:
        raise HTTPException(status_code=403, detail="File is not readable")
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Invalid path")
    if stat.S_ISDIR(st.st_mode):
        raise HTTPException(status_code=400, detail="Path points to a directory")
    if not stat.S_ISREG(st.st_mode):
        raise HTTPException(status_code=400, detail="Only regular files can be read")
    return target, st


def _fs_find_git_root(start: Path) -> str | None:
    directory = start
    for _ in range(50):
        try:
            if (directory / ".git").exists():
                return str(directory)
        except OSError:
            return None
        parent = directory.parent
        if parent == directory:
            return None
        directory = parent
    return None


def _fs_default_cwd() -> str:
    cfg_terminal = load_config().get("terminal") or {}
    raw = str(cfg_terminal.get("cwd") or os.environ.get("TERMINAL_CWD") or "").strip()
    if raw and raw not in {".", "auto", "cwd"}:
        try:
            candidate = Path(raw).expanduser().resolve(strict=False)
            if candidate.is_dir():
                return str(candidate)
        except (OSError, RuntimeError):
            pass
    return str(Path.cwd())


def _fs_git_branch(cwd: str) -> str:
    try:
        run_kwargs: Dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "timeout": 2,
            "check": False,
        }
        if sys.platform == "win32":
            run_kwargs["creationflags"] = windows_hide_flags()
        result = subprocess.run(
            ["git", "-C", cwd, "branch", "--show-current"],
            **run_kwargs,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _media_serve_roots() -> list[Path]:
    """Directories ``GET /api/media`` is allowed to read from.

    Confined to where the agent and attach pipeline actually write media on the
    gateway host — its images dir and cache subtree. This stops an authenticated
    client from reading image-extension files anywhere on disk (e.g. a renamed
    key or a screenshot outside the cache) merely because the suffix passes the
    allowlist.
    """
    home = get_hermes_home()
    roots = [home / "images", home / "screenshots", home / "cache"]
    out: list[Path] = []
    for root in roots:
        try:
            out.append(root.resolve())
        except (OSError, RuntimeError):
            continue
    return out


@app.get("/api/media")
async def get_media(request: Request, path: str):
    """Return a gateway-local image file as a base64 data URL.

    Lets remote clients (the desktop app over the network, or the web dashboard
    in a browser) display images the agent wrote to *this* machine's filesystem
    — they can't read the gateway's local disk directly.

    Auth-gated by the session token like every other /api route. Restricted to
    an image-extension allowlist, a size cap, AND the gateway's own media roots
    (resolved, symlink-safe) so it can't be used to read arbitrary files.
    """
    _reject_authenticated_filesystem_api(request)
    try:
        target = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="Invalid path")

    if target.suffix.lower() not in _MEDIA_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported media type")

    roots = _media_serve_roots()
    if not any(target == root or root in target.parents for root in roots):
        raise HTTPException(status_code=403, detail="Path outside media roots")

    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    if target.stat().st_size > _MEDIA_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File too large")

    encoded = base64.b64encode(target.read_bytes()).decode("ascii")
    return {"data_url": f"data:{_MEDIA_CONTENT_TYPES[target.suffix.lower()]};base64,{encoded}"}


def _canonical_path(path: Path, *, require_exists: bool = False) -> Path:
    try:
        return path.expanduser().resolve(strict=require_exists)
    except FileNotFoundError:
        if require_exists:
            raise HTTPException(status_code=404, detail="Path not found")
        raise
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="Invalid path")


def _ensure_managed_root(raw_path: str | Path) -> Path:
    root = Path(raw_path).expanduser()
    try:
        root.mkdir(parents=True, exist_ok=True)
        resolved = root.resolve()
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=500, detail=f"Managed files root is unavailable: {exc}")
    if not resolved.is_dir():
        raise HTTPException(status_code=500, detail="Managed files root is not a directory")
    return resolved


def _path_is_under(root: Path, target: Path) -> bool:
    return target == root or root in target.parents


def _path_text(raw_path: str | None) -> str:
    text = str(raw_path or "").strip()
    if "\x00" in text:
        raise HTTPException(status_code=400, detail="Invalid path")
    return text


def _local_dashboard_request(request: Request) -> bool:
    if getattr(request.app.state, "auth_required", False):
        return False
    host = (request.url.hostname or "").lower()
    client_host = (request.client.host if request.client else "").lower()
    local_hosts = {"", "localhost", "127.0.0.1", "::1", "testserver", "testclient"}
    return host in local_hosts or client_host in local_hosts


def _default_hermes_root_is_opt_data() -> bool:
    raw = os.environ.get("HERMES_HOME", "").strip()
    if not raw:
        return False
    try:
        from hermes_constants import get_default_hermes_root

        root = get_default_hermes_root().expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        root = Path(raw).expanduser().resolve(strict=False)
    return root == _HOSTED_MANAGED_FILES_ROOT


def _dashboard_local_update_managed_externally() -> bool:
    """Return true when the dashboard should not offer ``hermes update``.

    Containerized dashboards are updated by the outer launcher/image, not by an
    in-browser local update action. Keep this dashboard capability separate
    from install-method detection: manual git/pip installs inside containers can
    still behave like their actual install method in the CLI.

    However, when the install method is ``git`` (a bind-mounted checkout inside
    a container — e.g. the hermes-webui image sharing the Hermes source tree),
    the dashboard's ``hermes update`` button is the correct update path and
    should not be suppressed. Other containerized install methods remain
    externally managed unless their apply path is proven safe inside the
    running container filesystem.
    """
    if _default_hermes_root_is_opt_data():
        return True
    try:
        from hermes_constants import is_container

        if not is_container():
            return False
    except Exception:
        return False
    # We are inside a container, but the install may still be self-managed.
    # If the install method is git, the dashboard update button works against
    # the mounted checkout and should be offered. Keep pip blocked inside
    # containers: its apply path mutates the running container filesystem and
    # is not the bind-mounted checkout case this gate is meant to recover.
    try:
        method = detect_install_method(PROJECT_ROOT)
        if method == "git":
            return False
    except Exception:
        pass
    return True


def _managed_files_policy(request: Request, *, create_root: bool = True) -> ManagedFilesPolicy:
    raw_forced_root = os.environ.get(_MANAGED_FILES_ROOT_ENV, "").strip()
    if raw_forced_root:
        root = _ensure_managed_root(raw_forced_root) if create_root else _canonical_path(Path(raw_forced_root))
        return ManagedFilesPolicy(default_path=root, locked_root=root, can_change_path=False)

    # Remote/OAuth access does not imply a hosted container. Users can expose a
    # local dashboard through the auth gate (for example a macOS launchd install)
    # and still expect the Files page to browse their local home directory. Lock
    # to /opt/data only when the installation's Hermes root is actually /opt/data
    # (the container/hosted layout) or when HERMES_DASHBOARD_FILES_ROOT is set.
    if _default_hermes_root_is_opt_data():
        root = _ensure_managed_root(_HOSTED_MANAGED_FILES_ROOT) if create_root else _HOSTED_MANAGED_FILES_ROOT
        return ManagedFilesPolicy(default_path=root, locked_root=root, can_change_path=False)

    home = _canonical_path(Path.home())
    return ManagedFilesPolicy(default_path=home, locked_root=None, can_change_path=True)


def _resolve_managed_path(
    raw_path: str | None,
    request: Request,
    *,
    for_write: bool = False,
) -> tuple[ManagedFilesPolicy, Path, str]:
    policy = _managed_files_policy(request)
    text = _path_text(raw_path)
    root = policy.locked_root

    if root is not None and (not text or text in {".", "/"}):
        candidate = root
    elif not text:
        candidate = policy.default_path
    else:
        candidate = Path(text).expanduser()
        if root is not None and not candidate.is_absolute():
            if any(part == ".." for part in candidate.parts):
                raise HTTPException(status_code=400, detail="Path cannot contain '..'")
            candidate = root / candidate
        elif not candidate.is_absolute():
            raise HTTPException(status_code=400, detail="Path must be absolute")

    if ".." in candidate.parts:
        raise HTTPException(status_code=400, detail="Path cannot contain '..'")

    if for_write and not candidate.exists():
        parent = _canonical_path(candidate.parent)
        resolved = parent / candidate.name
    else:
        resolved = _canonical_path(candidate, require_exists=not for_write)

    if root is not None and not _path_is_under(root, resolved):
        raise HTTPException(status_code=403, detail="Path outside managed files root")

    return policy, resolved, str(resolved)


def _managed_response_meta(policy: ManagedFilesPolicy) -> Dict[str, Any]:
    locked_root = str(policy.locked_root) if policy.locked_root is not None else None
    return {
        "root": locked_root,
        "locked_root": locked_root,
        "can_change_path": policy.can_change_path,
    }


def _managed_file_entry(policy: ManagedFilesPolicy, target: Path) -> Dict[str, Any]:
    try:
        resolved = target.resolve()
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="Invalid path")
    if policy.locked_root is not None and not _path_is_under(policy.locked_root, resolved):
        raise HTTPException(status_code=403, detail="Path outside managed files root")

    try:
        st = resolved.stat()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not stat path: {exc}")

    is_dir = resolved.is_dir()
    mime_type = None if is_dir else (mimetypes.guess_type(resolved.name)[0] or "application/octet-stream")
    return {
        "name": target.name or resolved.name or str(resolved),
        "path": str(resolved),
        "is_directory": is_dir,
        "size": None if is_dir else st.st_size,
        "mtime": st.st_mtime,
        "mime_type": mime_type,
    }


def _decode_data_url(data_url: str) -> tuple[bytes, str]:
    text = (data_url or "").strip()
    if not text.startswith("data:") or "," not in text:
        raise HTTPException(status_code=400, detail="Upload payload must be a data URL")
    header, encoded = text.split(",", 1)
    mime_type = header[5:].split(";", 1)[0] or "application/octet-stream"
    if ";base64" not in header:
        raise HTTPException(status_code=400, detail="Upload payload must be base64 encoded")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="Upload payload is not valid base64")
    if len(data) > _MANAGED_FILE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File is too large")
    return data, mime_type


@app.get("/api/files")
async def list_managed_files(request: Request, path: Optional[str] = None):
    if _authenticated_owner_request(request):
        return await _proxy_authenticated_owner_http(request)
    _reject_authenticated_filesystem_api(request)
    policy, target, display_path = _resolve_managed_path(path, request)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")

    try:
        entries = [_managed_file_entry(policy, child) for child in target.iterdir()]
    except PermissionError:
        raise HTTPException(status_code=403, detail="Directory is not readable")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not read directory: {exc}")

    entries.sort(key=lambda item: (not item["is_directory"], str(item["name"]).lower()))
    locked_root = policy.locked_root
    parent = None
    if target.parent != target and (locked_root is None or target != locked_root):
        parent = str(target.parent)
    return {
        "path": display_path,
        "parent": parent,
        "entries": entries,
        **_managed_response_meta(policy),
    }


@app.get("/api/files/read")
async def read_managed_file(request: Request, path: str):
    if _authenticated_owner_request(request):
        return await _proxy_authenticated_owner_http(request)
    _reject_authenticated_filesystem_api(request)
    policy, target, display_path = _resolve_managed_path(path, request)
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file")

    try:
        size = target.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not stat file: {exc}")
    if size > _MANAGED_FILE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File is too large")

    mime_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    try:
        encoded = base64.b64encode(target.read_bytes()).decode("ascii")
    except PermissionError:
        raise HTTPException(status_code=403, detail="File is not readable")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not read file: {exc}")

    return {
        "name": target.name,
        "path": display_path,
        "size": size,
        "mime_type": mime_type,
        "data_url": f"data:{mime_type};base64,{encoded}",
        **_managed_response_meta(policy),
    }


@app.get("/api/files/download")
async def download_managed_file(
    request: Request,
    path: str,
    cwd: str | None = None,
    filename: str | None = None,
):
    """Stream a managed or session file as an attachment download.

    Remote clients (desktop app, browser dashboard) open agent-written files
    that live on *this* gateway's disk, not theirs. Auth-gated like every other
    managed-files route — ``auth_middleware`` additionally accepts the session
    token as a ``?token=`` query param here so a shell/browser-opened download
    (which cannot set the session header) still authenticates.
    for the same query-token precedent.
    """
    if _authenticated_owner_request(request):
        return await _proxy_authenticated_owner_http(request)
    _reject_authenticated_filesystem_api(request)
    if cwd is None:
        _policy, target, _display_path = _resolve_managed_path(path, request)
        target, size_info = _fs_regular_file(target)
    else:
        target, size_info = _fs_regular_file(_fs_path(path, cwd=cwd))
    if size_info.st_size > _MANAGED_FILE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File is too large")

    download_name = Path(str(filename or target.name)).name
    if not download_name or download_name in {".", ".."} or "\0" in download_name:
        download_name = target.name
    mime_type = mimetypes.guess_type(download_name)[0] or _fs_mime_type(target)

    return FileResponse(
        path=str(target),
        media_type=mime_type,
        filename=download_name,
        content_disposition_type="attachment",
    )


@app.post("/api/files/upload")
async def upload_managed_file(payload: ManagedFileUpload, request: Request):
    if _authenticated_owner_request(request):
        return await _proxy_authenticated_owner_http(request)
    _reject_authenticated_filesystem_api(request)
    policy, target, display_path = _resolve_managed_path(payload.path, request, for_write=True)
    if target.exists() and target.is_dir():
        raise HTTPException(status_code=409, detail="A directory already exists at that path")
    if target.exists() and not payload.overwrite:
        raise HTTPException(status_code=409, detail="File already exists")

    data, _mime_type = _decode_data_url(payload.data_url)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except PermissionError:
        raise HTTPException(status_code=403, detail="File is not writable")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not write file: {exc}")

    return {
        "ok": True,
        "entry": _managed_file_entry(policy, target),
        "path": display_path,
        **_managed_response_meta(policy),
    }


# Stream uploads to disk in fixed-size chunks. The legacy JSON endpoint above
# buffers the whole file as a base64 data URL in a JSON body, which (a) inflates
# the payload ~33%, (b) holds the entire file (plus its decoded copy) in memory,
# and (c) reliably trips upstream proxy body-size/timeout limits with a 502 on
# large backup archives (NS-501). This multipart endpoint reads the request body
# in 1 MiB chunks straight to a temp file, enforces the size cap as it goes, and
# atomically renames into place — constant memory, no base64 inflation.
_UPLOAD_CHUNK_BYTES = 1024 * 1024


@app.post("/api/files/upload-stream")
async def upload_managed_file_stream(request: Request):
    if hasattr(request, "app") and _authenticated_owner_request(request):
        return await _proxy_authenticated_owner_http(request)
    _reject_authenticated_filesystem_api(request)

    form = await request.form()
    file = form.get("file")
    path = form.get("path")
    overwrite_value = form.get("overwrite", "true")
    if (
        not callable(getattr(file, "read", None))
        or not callable(getattr(file, "close", None))
        or not isinstance(path, str)
    ):
        raise HTTPException(status_code=422, detail="Invalid upload form")
    overwrite = str(overwrite_value).strip().lower() in {"1", "true", "on", "yes"}
    policy, target, display_path = _resolve_managed_path(path, request, for_write=True)
    if target.exists() and target.is_dir():
        raise HTTPException(status_code=409, detail="A directory already exists at that path")
    if target.exists() and not overwrite:
        raise HTTPException(status_code=409, detail="File already exists")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        raise HTTPException(status_code=403, detail="File is not writable")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not create parent directory: {exc}")

    # Write to a sibling temp file first so a partial/aborted upload never
    # clobbers an existing file, then atomically rename into place.
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".upload", dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    total = 0
    renamed = False
    try:
        with os.fdopen(tmp_fd, "wb") as out:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MANAGED_FILE_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="File is too large")
                out.write(chunk)
        os.replace(tmp_path, target)
        renamed = True
    except HTTPException:
        raise
    except PermissionError:
        raise HTTPException(status_code=403, detail="File is not writable")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not write file: {exc}")
    finally:
        # Clean up the temp file on every non-success exit, including
        # BaseException paths the `except` clauses above don't catch — most
        # importantly asyncio.CancelledError when a browser aborts a large
        # upload mid-stream (the exact NS-501 scenario). os.replace clears
        # tmp_path on success, so only unlink when the rename didn't happen.
        if not renamed:
            tmp_path.unlink(missing_ok=True)
        await file.close()

    return {
        "ok": True,
        "entry": _managed_file_entry(policy, target),
        "path": display_path,
        **_managed_response_meta(policy),
    }


@app.post("/api/files/mkdir")
async def create_managed_directory(payload: ManagedDirectoryCreate, request: Request):
    if _authenticated_owner_request(request):
        return await _proxy_authenticated_owner_http(request)
    _reject_authenticated_filesystem_api(request)
    policy, target, display_path = _resolve_managed_path(payload.path, request, for_write=True)
    if target.exists() and not target.is_dir():
        raise HTTPException(status_code=409, detail="A file already exists at that path")

    try:
        target.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        raise HTTPException(status_code=403, detail="Directory is not writable")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not create directory: {exc}")

    return {
        "ok": True,
        "entry": _managed_file_entry(policy, target),
        "path": display_path,
        **_managed_response_meta(policy),
    }


@app.delete("/api/files")
async def delete_managed_file(payload: ManagedFileDelete, request: Request):
    if _authenticated_owner_request(request):
        return await _proxy_authenticated_owner_http(request)
    _reject_authenticated_filesystem_api(request)
    policy, target, display_path = _resolve_managed_path(payload.path, request)
    if policy.locked_root is not None and target == policy.locked_root:
        raise HTTPException(status_code=400, detail="Cannot delete the managed files root")
    if target.parent == target:
        raise HTTPException(status_code=400, detail="Cannot delete the filesystem root")
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")

    try:
        if target.is_dir():
            if payload.recursive:
                shutil.rmtree(target)
            else:
                target.rmdir()
        else:
            target.unlink()
    except OSError as exc:
        status_code = 409 if target.is_dir() and not payload.recursive else 500
        raise HTTPException(status_code=status_code, detail=f"Could not delete path: {exc}")

    return {"ok": True, "path": display_path, **_managed_response_meta(policy)}


@app.get("/api/fs/list")
async def fs_list(request: Request, path: str):
    _reject_authenticated_filesystem_api(request)
    target = _fs_path(path)
    try:
        entries = []
        with os.scandir(target) as scan:
            for entry in scan:
                if entry.name in _FS_READDIR_HIDDEN:
                    continue
                entries.append({
                    "name": entry.name,
                    "path": str(target / entry.name),
                    "isDirectory": entry.is_dir(follow_symlinks=False),
                })
        entries.sort(key=lambda item: (not item["isDirectory"], item["name"].lower(), item["name"]))
        return {"entries": entries}
    except FileNotFoundError:
        return {"entries": [], "error": "ENOENT"}
    except NotADirectoryError:
        return {"entries": [], "error": "ENOTDIR"}
    except PermissionError:
        return {"entries": [], "error": "EACCES"}
    except OSError as exc:
        return {"entries": [], "error": getattr(exc, "strerror", None) or "read-error"}


@app.get("/api/fs/read-text")
async def fs_read_text(request: Request, path: str):
    _reject_authenticated_filesystem_api(request)
    target, st = _fs_regular_file(_fs_path(path))
    if st.st_size > _FS_TEXT_SOURCE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File too large")
    bytes_to_read = min(st.st_size, _FS_TEXT_PREVIEW_MAX_BYTES)
    try:
        with target.open("rb") as handle:
            data = handle.read(bytes_to_read)
    except PermissionError:
        raise HTTPException(status_code=403, detail="File is not readable")
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "File read failed")
    return {
        "binary": _fs_looks_binary(data[:4096]),
        "byteSize": st.st_size,
        "language": _FS_PREVIEW_LANGUAGE_BY_EXT.get(target.suffix.lower(), "text"),
        "mimeType": _fs_mime_type(target),
        "path": str(target),
        "text": data.decode("utf-8", errors="replace"),
        "truncated": st.st_size > _FS_TEXT_PREVIEW_MAX_BYTES,
    }


class FsWriteText(BaseModel):
    path: str
    content: str


@app.post("/api/fs/write-text")
async def fs_write_text(request: Request, payload: FsWriteText):
    """Overwrite (or create) a UTF-8 text file for the in-app spot editor.

    Mirrors the local Electron ``hermes:fs:writeText`` hardening: the path is
    resolved + validated by ``_fs_path``, the parent directory must already
    exist (we never build directory trees), only regular files may be replaced,
    and the payload is size-capped. The write is staged to a sibling temp file
    and ``os.replace``-d into place so a crash mid-write can't truncate the
    original. Stale-on-disk detection is the client's job (re-read before save),
    so both transports behave identically.
    """
    _reject_authenticated_filesystem_api(request)
    target = _fs_path(payload.path)
    text = payload.content or ""
    if len(text.encode("utf-8")) > _FS_TEXT_WRITE_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Content too large")

    try:
        st: Optional[os.stat_result] = target.stat()
    except FileNotFoundError:
        st = None
    except PermissionError:
        raise HTTPException(status_code=403, detail="File is not writable")
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Invalid path")

    if st is not None and stat.S_ISDIR(st.st_mode):
        raise HTTPException(status_code=400, detail="Path points to a directory")
    if st is not None and not stat.S_ISREG(st.st_mode):
        raise HTTPException(status_code=400, detail="Only regular files can be written")
    if not target.parent.is_dir():
        raise HTTPException(status_code=400, detail="Parent directory does not exist")

    tmp = target.with_name(f".{target.name}.hermes-tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    except PermissionError:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=403, detail="File is not writable")
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Could not write file: {exc}")

    return {"ok": True, "path": str(target), "byteSize": len(text.encode("utf-8"))}


@app.get("/api/fs/read-data-url")
async def fs_read_data_url(request: Request, path: str, cwd: str | None = None):
    if _authenticated_owner_request(request):
        return await _proxy_authenticated_owner_http(request)
    _reject_authenticated_filesystem_api(request)
    target, st = _fs_regular_file(_fs_path(path, cwd=cwd))
    if st.st_size > _FS_DATA_URL_MAX_BYTES:
        raise HTTPException(status_code=413, detail="File too large")
    try:
        encoded = base64.b64encode(target.read_bytes()).decode("ascii")
    except PermissionError:
        raise HTTPException(status_code=403, detail="File is not readable")
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "File read failed")
    return {"dataUrl": f"data:{_fs_mime_type(target)};base64,{encoded}"}


@app.get("/api/generated-images/{filename}")
async def generated_image_file(request: Request, filename: str):
    _reject_authenticated_filesystem_api(request)
    if not filename or filename in {".", ".."} or "/" in filename or "\\" in filename or "\0" in filename:
        raise HTTPException(status_code=400, detail="Invalid image filename")
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    roots = (home / "images", home / "cache" / "images")
    for root in roots:
        target = (root / filename).resolve(strict=False)
        try:
            target.relative_to(root.resolve(strict=False))
        except ValueError:
            continue
        try:
            st = target.stat()
        except FileNotFoundError:
            continue
        except NotADirectoryError:
            continue
        except PermissionError:
            raise HTTPException(status_code=403, detail="File is not readable")
        except OSError as exc:
            raise HTTPException(status_code=400, detail=str(exc) or "Invalid path")
        if stat.S_ISDIR(st.st_mode):
            raise HTTPException(status_code=400, detail="Path points to a directory")
        if not stat.S_ISREG(st.st_mode):
            raise HTTPException(status_code=400, detail="Only regular files can be read")
        mime_type = _fs_mime_type(target)
        if not mime_type.startswith("image/"):
            raise HTTPException(status_code=400, detail="File is not an image")
        return FileResponse(
            target,
            media_type=mime_type,
            filename=filename,
            content_disposition_type="inline",
            headers={"Cache-Control": "private, max-age=31536000, immutable"},
        )
    raise HTTPException(status_code=404, detail="File not found")


@app.get("/api/fs/git-root")
async def fs_git_root(request: Request, path: str):
    _reject_authenticated_filesystem_api(request)
    target = _fs_path(path)
    try:
        st = target.stat()
        start = target if stat.S_ISDIR(st.st_mode) else target.parent
    except OSError:
        start = target
    return {"root": _fs_find_git_root(start)}


@app.get("/api/fs/default-cwd")
async def fs_default_cwd(request: Request):
    _reject_authenticated_filesystem_api(request)
    cwd = _fs_default_cwd()
    return {"cwd": cwd, "branch": _fs_git_branch(cwd)}


# ---------------------------------------------------------------------------
# Git ops — the remote half of the desktop coding rail + review pane.
#
# The desktop runs these as Electron-local git on the user's machine; over a
# remote gateway that's the wrong filesystem, so we mirror them here (same auth
# gate + path hardening as /api/fs). Logic lives in ``hermes_cli.web_git``;
# these are thin, executor-offloaded wrappers (git/gh can block).
# ---------------------------------------------------------------------------

from hermes_cli import web_git as _web_git  # noqa: E402


async def _git_op(fn, *args):
    """Run a (blocking) git op off the event loop; map a failed mutation to 400."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, fn, *args)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "git operation failed")


def _git_path(path: str) -> str:
    return str(_fs_path(path))


class GitPathBody(BaseModel):
    path: str


class GitFileBody(BaseModel):
    path: str
    file: Optional[str] = None


class GitCommitBody(BaseModel):
    path: str
    message: str
    push: bool = False


class GitWorktreeAddBody(BaseModel):
    path: str
    name: Optional[str] = None
    branch: Optional[str] = None
    base: Optional[str] = None
    existingBranch: Optional[str] = None


class GitWorktreeRemoveBody(BaseModel):
    path: str
    worktreePath: str
    force: bool = False


class GitBranchSwitchBody(BaseModel):
    path: str
    branch: str


@app.get("/api/git/status")
async def git_status_route(request: Request, path: str):
    _reject_authenticated_control_plane_owner_surface("Git APIs")
    return await _git_op(_web_git.repo_status, _git_path(path))


@app.get("/api/git/worktrees")
async def git_worktrees_route(request: Request, path: str):
    _reject_authenticated_control_plane_owner_surface("Git APIs")
    return {"worktrees": await _git_op(_web_git.worktree_list, _git_path(path))}


@app.get("/api/git/branches")
async def git_branches_route(request: Request, path: str):
    _reject_authenticated_control_plane_owner_surface("Git APIs")
    return {"branches": await _git_op(_web_git.branch_list, _git_path(path))}


@app.get("/api/git/review/list")
async def git_review_list_route(request: Request, path: str, scope: str = "uncommitted", base: Optional[str] = None):
    _reject_authenticated_control_plane_owner_surface("Git APIs")
    return await _git_op(_web_git.review_list, _git_path(path), scope, base)


@app.get("/api/git/review/diff")
async def git_review_diff_route(
    request: Request, path: str, file: str, scope: str = "uncommitted", base: Optional[str] = None, staged: bool = False
):
    _reject_authenticated_control_plane_owner_surface("Git APIs")
    return {"diff": await _git_op(_web_git.review_diff, _git_path(path), file, scope, base, staged)}


@app.get("/api/git/file-diff")
async def git_file_diff_route(request: Request, path: str, file: str):
    _reject_authenticated_control_plane_owner_surface("Git APIs")
    return {"diff": await _git_op(_web_git.file_diff_vs_head, _git_path(path), file)}


@app.get("/api/git/review/commit-context")
async def git_commit_context_route(request: Request, path: str):
    _reject_authenticated_control_plane_owner_surface("Git APIs")
    return await _git_op(_web_git.review_commit_context, _git_path(path))


@app.get("/api/git/review/rev-parse")
async def git_rev_parse_route(request: Request, path: str, ref: Optional[str] = None):
    _reject_authenticated_control_plane_owner_surface("Git APIs")
    return {"sha": await _git_op(_web_git.review_rev_parse, _git_path(path), ref)}


@app.get("/api/git/review/ship-info")
async def git_ship_info_route(request: Request, path: str):
    _reject_authenticated_control_plane_owner_surface("Git APIs")
    return await _git_op(_web_git.review_ship_info, _git_path(path))


@app.post("/api/git/review/stage")
async def git_stage_route(request: Request, body: GitFileBody):
    _reject_authenticated_control_plane_owner_surface("Git APIs")
    return await _git_op(_web_git.review_stage, _git_path(body.path), body.file)


@app.post("/api/git/review/unstage")
async def git_unstage_route(request: Request, body: GitFileBody):
    _reject_authenticated_control_plane_owner_surface("Git APIs")
    return await _git_op(_web_git.review_unstage, _git_path(body.path), body.file)


@app.post("/api/git/review/revert")
async def git_revert_route(request: Request, body: GitFileBody):
    _reject_authenticated_control_plane_owner_surface("Git APIs")
    return await _git_op(_web_git.review_revert, _git_path(body.path), body.file)


@app.post("/api/git/review/commit")
async def git_commit_route(request: Request, body: GitCommitBody):
    _reject_authenticated_control_plane_owner_surface("Git APIs")
    return await _git_op(_web_git.review_commit, _git_path(body.path), body.message, body.push)


@app.post("/api/git/review/push")
async def git_push_route(request: Request, body: GitPathBody):
    _reject_authenticated_control_plane_owner_surface("Git APIs")
    return await _git_op(_web_git.review_push, _git_path(body.path))


@app.post("/api/git/review/create-pr")
async def git_create_pr_route(request: Request, body: GitPathBody):
    _reject_authenticated_control_plane_owner_surface("Git APIs")
    return await _git_op(_web_git.review_create_pr, _git_path(body.path))


@app.post("/api/git/worktree/add")
async def git_worktree_add_route(request: Request, body: GitWorktreeAddBody):
    _reject_authenticated_control_plane_owner_surface("Git APIs")
    options = {
        key: value
        for key, value in {
            "name": body.name,
            "branch": body.branch,
            "base": body.base,
            "existingBranch": body.existingBranch,
        }.items()
        if value
    }
    return await _git_op(_web_git.worktree_add, _git_path(body.path), options)


@app.post("/api/git/worktree/remove")
async def git_worktree_remove_route(request: Request, body: GitWorktreeRemoveBody):
    _reject_authenticated_control_plane_owner_surface("Git APIs")
    return await _git_op(
        _web_git.worktree_remove, _git_path(body.path), _git_path(body.worktreePath), body.force
    )


@app.post("/api/git/branch/switch")
async def git_branch_switch_route(request: Request, body: GitBranchSwitchBody):
    _reject_authenticated_control_plane_owner_surface("Git APIs")
    return await _git_op(_web_git.branch_switch, _git_path(body.path), body.branch)


@app.get("/api/status")
async def get_status(profile: Optional[str] = None):
    status_scope = None
    requested_profile = (profile or "").strip()
    # Plain /api/status stays the machine-level public liveness probe. The
    # dashboard adds ?profile= in local/profile-manager mode when its management
    # switcher targets another profile, so its gateway badge reflects the selected
    # profile.  In authenticated owner mode this public endpoint must not let an
    # unauthenticated caller select a legacy profile/home; owner-sensitive status
    # must be routed through an authenticated Owner Worker surface instead.
    #
    # Use the config-only (contextvar) scope, NOT _profile_scope: this handler
    # awaits the remote-health probe, and _profile_scope swaps process-global
    # skills-module attributes that a concurrent request would cross-restore
    # across that await. Status only resolves get_hermes_home() at call time
    # (config/env/gateway state), which the task-local contextvar covers.
    if requested_profile and requested_profile.lower() != "current":
        if getattr(app.state, "auth_required", False):
            raise HTTPException(status_code=403, detail="profile status is not available in authenticated mode")
        status_scope = _config_profile_scope(requested_profile)
        status_scope.__enter__()

    try:
        current_ver, latest_ver = check_config_version()
        # --- Gateway liveness detection ---
        # Try local PID check first (same-host).  If that fails and a remote
        # GATEWAY_HEALTH_URL is configured, probe the gateway over HTTP so the
        # dashboard works when the gateway runs in a separate container.
        gateway_pid = get_running_pid()
        gateway_running = gateway_pid is not None
        remote_health_body: dict | None = None

        if not gateway_running and _GATEWAY_HEALTH_URL:
            loop = asyncio.get_running_loop()
            alive, remote_health_body = await loop.run_in_executor(
                None, _probe_gateway_health
            )
            if alive:
                gateway_running = True
                # PID from the remote container (display only — not locally valid)
                if remote_health_body:
                    gateway_pid = remote_health_body.get("pid")

        gateway_state = None
        gateway_platforms: dict = {}
        gateway_exit_reason = None
        gateway_updated_at = None
        configured_gateway_platforms: set[str] | None = None
        try:
            from gateway.config import load_gateway_config

            gateway_config = load_gateway_config()
            configured_gateway_platforms = {
                platform.value for platform in gateway_config.get_connected_platforms()
            }
        except Exception:
            configured_gateway_platforms = None

        # Prefer the detailed health endpoint response (has full state) when the
        # local runtime status file is absent or stale (cross-container).
        local_runtime = read_runtime_status()
        runtime = local_runtime
        if runtime is None and remote_health_body and remote_health_body.get("gateway_state"):
            runtime = remote_health_body
        # The runtime-status PID fallback validates liveness with a local
        # os.kill() probe, so it must only run against the LOCAL status file —
        # never the remote health body, whose PID belongs to another host and
        # is display-only. (Running os.kill on a remote PID is both wrong and
        # trips the test live-system guard.)
        if not gateway_running and local_runtime is not None:
            runtime_pid = get_runtime_status_running_pid(local_runtime)
            if runtime_pid is not None:
                gateway_running = True
                gateway_pid = runtime_pid

        if runtime:
            gateway_state = runtime.get("gateway_state")
            gateway_platforms = runtime.get("platforms") or {}
            if configured_gateway_platforms is not None:
                gateway_platforms = {
                    key: value
                    for key, value in gateway_platforms.items()
                    if key in configured_gateway_platforms
                }
            gateway_exit_reason = runtime.get("exit_reason")
            gateway_updated_at = runtime.get("updated_at")
            if not gateway_running:
                gateway_state = gateway_state if gateway_state in {"stopped", "startup_failed"} else "stopped"
                gateway_platforms = {}
            elif gateway_running and remote_health_body is not None:
                # The health probe confirmed the gateway is alive, but the local
                # runtime status file may be stale (cross-container).  Override
                # stopped/None state so the dashboard shows the correct badge.
                if gateway_state in {None, "stopped"}:
                    gateway_state = "running"

        # If there was no runtime info at all but the health probe confirmed alive,
        # ensure we still report the gateway as running (no shared volume scenario).
        if gateway_running and gateway_state is None and remote_health_body is not None:
            gateway_state = "running"

        active_sessions = 0
        if not getattr(app.state, "auth_required", False):
            try:
                from hermes_state import SessionDB
                db = SessionDB()
                try:
                    sessions = db.list_sessions_rich(limit=50)
                    now = time.time()
                    active_sessions = sum(
                        1 for s in sessions
                        if s.get("ended_at") is None
                        and (now - s.get("last_active", s.get("started_at", 0))) < 300
                    )
                finally:
                    db.close()
            except Exception:
                pass

        # Busy/drainable readout (NAS lifecycle-safety gate).  active_agents is
        # the in-flight gateway-turn count the gateway now persists at every
        # turn boundary; gateway_busy/gateway_drainable are derived from it +
        # liveness via the single shared contract in gateway.status.  Liveness
        # keys off gateway_running (a live PID/health probe), NEVER
        # gateway_updated_at — a healthy idle gateway never advances that.
        active_agents = parse_active_agents((runtime or {}).get("active_agents", 0))
        gateway_busy = derive_gateway_busy(
            gateway_running=gateway_running,
            gateway_state=gateway_state,
            active_agents=active_agents,
        )
        gateway_drainable = derive_gateway_drainable(
            gateway_running=gateway_running,
            gateway_state=gateway_state,
        )
        # Resolved drain timeout (seconds) so NAS can size its poll deadline
        # without out-of-band knowledge.  Offload to a thread: on a cold
        # Windows install the first import of hermes_cli.gateway blocks the
        # asyncio event loop for 15-30s (.pyc compilation + Defender scans),
        # exceeding the desktop handshake's 15s socket timeout.  After the
        # first call the module is in sys.modules and run_in_executor returns
        # in microseconds.
        restart_drain_timeout = await asyncio.get_running_loop().run_in_executor(
            None, _resolve_restart_drain_timeout
        )

        # Dashboard auth gate (Phase 7): surface whether the gate is engaged
        # and which providers are registered so ``hermes status`` and the
        # SPA's StatusPage can show "OAuth gate ON via Nous Research" or
        # "loopback only — no auth gate" with no extra round trips.
        auth_required = bool(getattr(app.state, "auth_required", False))
        auth_providers: list[str] = []
        try:
            from hermes_cli.dashboard_auth import list_providers as _list_providers
            auth_providers = [p.name for p in _list_providers()]
        except Exception:
            # Module not importable yet (early startup) — leave as [].
            pass

        # Always-public liveness + auth-gate shape. Safe for external uptime
        # probes (NAS's wildcard-subdomain liveness probe), the SPA's pre-login
        # bootstrap, and anyone who can curl the host — i.e. exactly the audience
        # ``PUBLIC_API_PATHS`` documents this endpoint as serving.
        status = {
            "version": __version__,
            "release_date": __release_date__,
            "config_version": current_ver,
            "latest_config_version": latest_ver,
            "can_update_hermes": not _dashboard_local_update_managed_externally(),
            "gateway_running": gateway_running,
            "gateway_state": gateway_state,
            "gateway_platforms": gateway_platforms,
            "gateway_exit_reason": gateway_exit_reason,
            "gateway_updated_at": gateway_updated_at,
            "active_agents": active_agents,
            "gateway_busy": gateway_busy,
            "gateway_drainable": gateway_drainable,
            "restart_drain_timeout": restart_drain_timeout,
            "active_sessions": active_sessions,
            "auth_required": auth_required,
            "auth_providers": auth_providers,
        }

        # Absolute host paths, the gateway PID, and the internal gateway health
        # URL are deployment recon a liveness probe never needs. ``/api/status``
        # is in ``PUBLIC_API_PATHS`` so it bypasses dashboard auth; on a
        # network-exposed (gated) bind that means *any* unauthenticated caller
        # reaches it, and leaking host metadata there contradicts the allowlist's
        # own contract ("version, gateway state, active session count, and the
        # dashboard auth-gate shape. No bodies, no session content, no secrets").
        # Isolated tests may disable the gate; production startup always enables it.
        if not auth_required:
            status.update({
                "hermes_home": str(get_hermes_home()),
                "config_path": str(get_config_path()),
                "env_path": str(get_env_path()),
                "gateway_pid": gateway_pid,
                "gateway_health_url": _GATEWAY_HEALTH_URL,
            })

        return status
    finally:
        if status_scope is not None:
            status_scope.__exit__(*sys.exc_info())


_WINDOWS_11_MIN_BUILD = 22000


def _windows_build_number(version: str, platform_label: str) -> Optional[int]:
    """Extract the Windows NT build number from stdlib platform strings."""
    for value in (version or "", platform_label or ""):
        match = re.search(r"(?:^|[^\d])10\.0\.(\d{5,})(?:[^\d]|$)", value)
        if not match:
            continue
        try:
            return int(match.group(1))
        except ValueError:
            continue
    return None


def _display_system_platform(
    *,
    system: str,
    release: str,
    version: str,
    platform_label: str,
) -> Dict[str, str]:
    """Return host OS fields for display while preserving stdlib detail."""
    if system == "Windows" and release == "10":
        build = _windows_build_number(version, platform_label)
        if build is not None and build >= _WINDOWS_11_MIN_BUILD:
            platform_label = re.sub(
                r"^Windows-10(?=-)",
                "Windows-11",
                platform_label,
                count=1,
            )
            release = "11"

    return {
        "os": system,
        "os_release": release,
        "os_version": version,
        "platform": platform_label,
    }


@app.get("/api/system/stats")
async def get_system_stats():
    """Host + process system stats for the System page.

    OS / Python / host identity from stdlib; CPU / memory / disk / uptime from
    psutil when available, with graceful degradation when it isn't.  Read-only
    and non-sensitive (no env values, no paths beyond the hermes home root).
    """
    import platform as _platform

    info: Dict[str, Any] = {
        **_display_system_platform(
            system=_platform.system(),
            release=_platform.release(),
            version=_platform.version(),
            platform_label=_platform.platform(),
        ),
        "arch": _platform.machine(),
        "hostname": _platform.node(),
        "python_version": _platform.python_version(),
        "python_impl": _platform.python_implementation(),
        "hermes_version": __version__,
        "cpu_count": os.cpu_count(),
    }

    # psutil enriches the picture when present; everything below is optional.
    try:
        import psutil  # type: ignore

        vm = psutil.virtual_memory()
        info["memory"] = {
            "total": vm.total,
            "available": vm.available,
            "used": vm.used,
            "percent": vm.percent,
        }
        try:
            du = psutil.disk_usage(str(get_hermes_home()))
            info["disk"] = {
                "total": du.total,
                "used": du.used,
                "free": du.free,
                "percent": du.percent,
            }
        except Exception:
            pass
        try:
            info["cpu_percent"] = psutil.cpu_percent(interval=0.1)
            la = getattr(psutil, "getloadavg", None)
            if la:
                info["load_avg"] = list(la())
        except Exception:
            pass
        try:
            boot = psutil.boot_time()
            info["uptime_seconds"] = int(time.time() - boot)
        except Exception:
            pass
        try:
            proc = psutil.Process()
            info["process"] = {
                "pid": proc.pid,
                "rss": proc.memory_info().rss,
                "create_time": int(proc.create_time()),
                "num_threads": proc.num_threads(),
            }
        except Exception:
            pass
        info["psutil"] = True
    except Exception:
        info["psutil"] = False
        # stdlib-only fallbacks for load average + uptime where the kernel
        # exposes them.
        try:
            info["load_avg"] = list(os.getloadavg())
        except (OSError, AttributeError):
            pass

    return info


# ---------------------------------------------------------------------------
# Curator endpoints — background skill-maintenance status + controls.
#
# The curator periodically reviews skills (archive stale, prune, pin).  The
# dashboard surfaces its state and the pause/resume/run-now controls that
# `hermes curator` exposes.
# ---------------------------------------------------------------------------


@app.get("/api/curator")
async def get_curator_status():
    try:
        from agent import curator
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Curator unavailable: {exc}")
    try:
        state = curator.load_state()
    except Exception:
        state = {}
    return {
        "enabled": _safe_call(curator, "is_enabled", True),
        "paused": _safe_call(curator, "is_paused", False),
        "interval_hours": _safe_call(curator, "get_interval_hours", None),
        "last_run_at": state.get("last_run_at"),
        "min_idle_hours": _safe_call(curator, "get_min_idle_hours", None),
        "stale_after_days": _safe_call(curator, "get_stale_after_days", None),
        "archive_after_days": _safe_call(curator, "get_archive_after_days", None),
    }


class CuratorPause(BaseModel):
    paused: bool


@app.put("/api/curator/paused")
async def set_curator_paused(body: CuratorPause):
    from agent import curator

    curator.set_paused(bool(body.paused))
    return {"ok": True, "paused": bool(body.paused)}


@app.post("/api/curator/run")
async def run_curator():
    """Trigger a curator review now (backgrounded; tail via action status)."""
    try:
        proc = _spawn_hermes_action(["curator", "run"], "curator-run")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to run curator: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "curator-run"}


@app.get("/api/learning/graph")
async def get_learning_graph(profile: Optional[str] = None):
    """Learning graph payload for the desktop panel.

    Profile-scoped view of learned, non-base skills plus memory chunks, with
    graph links derived from skill relations and memory-skill overlap.
    """
    try:
        from agent.learning_graph import build_learning_graph

        with _profile_scope(profile):
            return build_learning_graph()
    except Exception:
        _log.exception("GET /api/learning/graph failed")
        raise HTTPException(status_code=500, detail="Failed to build learning graph")


class LearningNodeRef(BaseModel):
    id: str
    profile: Optional[str] = None


class LearningNodeEdit(BaseModel):
    id: str
    content: str
    profile: Optional[str] = None


@app.get("/api/learning/node")
async def get_learning_node(id: str, profile: Optional[str] = None):
    """Current content of a journey node (skill SKILL.md or memory chunk), for an edit prefill."""
    from agent.learning_mutations import node_detail

    with _profile_scope(profile):
        res = node_detail(id)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("message", "not found"))
    return res


@app.delete("/api/learning/node")
async def delete_learning_node(body: LearningNodeRef):
    """Delete a journey node — skills are archived (restorable), memories removed."""
    from agent.learning_mutations import delete_node

    with _profile_scope(body.profile):
        res = delete_node(body.id)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("message", "delete failed"))
    return res


@app.put("/api/learning/node")
async def update_learning_node(body: LearningNodeEdit):
    """Rewrite a journey node's content (SKILL.md or memory chunk)."""
    from agent.learning_mutations import edit_node

    with _profile_scope(body.profile):
        res = edit_node(body.id, body.content)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("message", "edit failed"))
    return res


def _safe_call(mod, fn_name: str, default):
    try:
        fn = getattr(mod, fn_name, None)
        return fn() if callable(fn) else default
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Portal endpoint — Nous Portal auth + Tool Gateway routing status (read-only).
# ---------------------------------------------------------------------------


@app.get("/api/portal")
async def get_portal_status():
    cfg = load_config() or {}
    auth: Dict[str, Any] = {}
    try:
        from hermes_cli.auth import get_nous_auth_status

        auth = get_nous_auth_status() or {}
    except Exception:
        auth = {}

    features = []
    try:
        from hermes_cli.nous_subscription import get_nous_subscription_features

        feats = get_nous_subscription_features(cfg)
        if feats is not None:
            for feat in feats.items():
                if getattr(feat, "managed_by_nous", False):
                    state = "via Nous Portal"
                elif getattr(feat, "active", False) and getattr(feat, "current_provider", None):
                    state = feat.current_provider
                elif getattr(feat, "active", False):
                    state = "active"
                else:
                    state = "not configured"
                features.append({"label": getattr(feat, "label", ""), "state": state})
    except Exception:
        _log.exception("portal features failed")

    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    return {
        "logged_in": bool(auth.get("logged_in")),
        "portal_url": auth.get("portal_base_url"),
        "inference_url": auth.get("inference_base_url"),
        "provider": str((model_cfg or {}).get("provider") or ""),
        "subscription_url": "https://portal.nousresearch.com/manage-subscription",
        "features": features,
    }


# ---------------------------------------------------------------------------
# Diagnostics: prompt-size, support dump, debug upload, config migrate.
# All produce text output, so they spawn background actions tailed via
# /api/actions/<name>/status.
# ---------------------------------------------------------------------------


@app.post("/api/ops/prompt-size")
async def run_prompt_size():
    try:
        proc = _spawn_hermes_action(["prompt-size"], "prompt-size")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "prompt-size"}


@app.post("/api/ops/dump")
async def run_dump():
    try:
        proc = _spawn_hermes_action(["dump"], "dump")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "dump"}


@app.post("/api/ops/config-migrate")
async def run_config_migrate():
    try:
        proc = _spawn_hermes_action(["config", "migrate"], "config-migrate")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "config-migrate"}


class DebugShareRequest(BaseModel):
    # Redaction is ON by default — force-mode scrubs credential-shaped tokens
    # out of log content before it leaves the machine. The toggle exists so an
    # operator who knows the logs are clean can opt out for fuller fidelity.
    redact: bool = True
    # Recent log lines included in the summary tail (full logs are separate).
    lines: int = 200


@app.post("/api/ops/debug-share")
async def run_debug_share_endpoint(body: DebugShareRequest | None = None):
    """Upload a redacted debug report + full logs and return the paste URLs.

    Unlike the other diagnostics actions (doctor, dump, prompt-size) this is
    *synchronous*: the whole point of ``debug share`` is the set of shareable
    URLs it produces, so we run the upload in a worker thread and return the
    structured ``{urls, failures, redacted, ...}`` payload directly. The
    dashboard renders those as real, copyable links instead of scraping a log
    tail. Pastes auto-delete after 6 hours (handled inside the share core).
    """
    from hermes_cli.debug import build_debug_share

    req = body or DebugShareRequest()
    try:
        result = await asyncio.to_thread(
            build_debug_share,
            log_lines=max(1, min(int(req.lines), 5000)),
            redact=bool(req.redact),
        )
    except RuntimeError as exc:
        # Required summary-report upload failed (offline / paste service down).
        raise HTTPException(status_code=502, detail=f"Upload failed: {exc}")
    except Exception as exc:
        _log.exception("debug share failed")
        raise HTTPException(status_code=500, detail=f"Failed: {exc}")

    return {
        "ok": True,
        "urls": result.urls,
        "failures": result.failures,
        "redacted": result.redacted,
        "auto_delete_seconds": result.auto_delete_seconds,
    }


# ---------------------------------------------------------------------------
# Gateway + update actions (invoked from the Status page).
#
# Both commands are spawned as detached subprocesses so the HTTP request
# returns immediately.  stdin is closed (``DEVNULL``) so any stray ``input()``
# calls fail fast with EOF rather than hanging forever.  stdout/stderr are
# streamed to a per-action log file under ``~/.hermes/logs/<action>.log`` so
# the dashboard can tail them back to the user.
# ---------------------------------------------------------------------------

_ACTION_LOG_DIR: Path = get_hermes_home() / "logs"

# Short ``name`` (from the URL) → absolute log file path.
_ACTION_LOG_FILES: Dict[str, str] = {
    "gateway-restart": "gateway-restart.log",
    "gateway-start": "gateway-start.log",
    "gateway-stop": "gateway-stop.log",
    "hermes-update": "hermes-update.log",
    "doctor": "action-doctor.log",
    "security-audit": "action-security-audit.log",
    "backup": "action-backup.log",
    "import": "action-import.log",
    "checkpoints-prune": "action-checkpoints-prune.log",
    "skills-install": "action-skills-install.log",
    "skills-uninstall": "action-skills-uninstall.log",
    "skills-update": "action-skills-update.log",
    "curator-run": "action-curator-run.log",
    "prompt-size": "action-prompt-size.log",
    "dump": "action-dump.log",
    "config-migrate": "action-config-migrate.log",
    "tools-post-setup": "action-tools-post-setup.log",
}

# ``name`` → most recently spawned Popen handle.  Used so ``status`` can
# report liveness and exit code without shelling out to ``ps``.
_ACTION_PROCS: Dict[str, subprocess.Popen] = {}
_ACTION_COMMANDS: Dict[str, Tuple[str, ...]] = {}

# ``name`` → completed synthetic action result for actions the server handled
# without spawning a subprocess (for example, unsupported Docker updates).
_ACTION_RESULTS: Dict[str, Dict[str, Any]] = {}


def _record_completed_action(name: str, message: str, exit_code: int = 1) -> None:
    """Record a non-spawned action result and write it to the action log."""
    log_file_name = _ACTION_LOG_FILES[name]
    _ACTION_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _ACTION_LOG_DIR / log_file_name
    with open(log_path, "ab", buffering=0) as log_file:
        log_file.write(
            f"\n=== {name} completed {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode()
        )
        log_file.write(message.encode("utf-8", errors="replace"))
        if not message.endswith("\n"):
            log_file.write(b"\n")
    _ACTION_PROCS.pop(name, None)
    _ACTION_COMMANDS.pop(name, None)
    _ACTION_RESULTS[name] = {"exit_code": exit_code, "pid": None}


def _dashboard_spawn_executable() -> str:
    """Prefer pythonw.exe for detached dashboard actions on Windows."""
    if sys.platform != "win32":
        return sys.executable
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.isfile(pythonw):
            return pythonw
    return exe


def _spawn_hermes_action(subcommand: List[str], name: str) -> subprocess.Popen:
    """Spawn ``hermes <subcommand>`` detached and record the Popen handle.

    Uses the running interpreter's ``hermes_cli.main`` module so the action
    inherits the same venv/PYTHONPATH the web server is using.
    """
    log_file_name = _ACTION_LOG_FILES[name]
    _ACTION_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = _ACTION_LOG_DIR / log_file_name
    log_file = open(log_path, "ab", buffering=0)
    log_file.write(
        f"\n=== {name} started {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode()
    )

    cmd = [_dashboard_spawn_executable(), "-m", "hermes_cli.main", *subcommand]

    popen_kwargs: Dict[str, Any] = {
        "cwd": str(PROJECT_ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
        "env": {**os.environ, "HERMES_NONINTERACTIVE": "1"},
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = windows_detach_flags()
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **popen_kwargs)
    # The child inherits its own duplicated fd for stdout/stderr, so the
    # parent's handle can be released immediately — otherwise we leak one
    # fd per spawned action.
    log_file.close()
    _ACTION_RESULTS.pop(name, None)
    _ACTION_COMMANDS[name] = tuple(subcommand)
    _ACTION_PROCS[name] = proc
    return proc


def _tail_lines(path: Path, n: int) -> List[str]:
    """Return the last ``n`` lines of ``path``.  Reads the whole file — fine
    for our small per-action logs.  Binary-decoded with ``errors='replace'``
    so log corruption doesn't 500 the endpoint."""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = text.splitlines()
    return lines[-n:] if n > 0 else lines


@app.post("/api/hermes/update")
async def update_hermes():
    """Kick off ``hermes update`` in the background."""
    if _dashboard_local_update_managed_externally():
        message = (
            "Hermes updates are managed outside this dashboard in "
            "containerized environments. The built-in local updater is "
            "disabled here."
        )
        _record_completed_action("hermes-update", message, exit_code=1)
        return {
            "ok": False,
            "pid": None,
            "name": "hermes-update",
            "error": "dashboard_update_managed_externally",
            "message": message,
            "update_command": "managed outside dashboard",
        }

    install_method = detect_install_method(PROJECT_ROOT)
    if install_method == "docker":
        message = format_docker_update_message()
        _record_completed_action("hermes-update", message, exit_code=1)
        return {
            "ok": False,
            "pid": None,
            "name": "hermes-update",
            "error": "docker_update_unsupported",
            "message": message,
            "update_command": recommended_update_command_for_method(install_method),
        }

    try:
        proc = _spawn_hermes_action(["update"], "hermes-update")
    except Exception as exc:
        _log.exception("Failed to spawn hermes update")
        raise HTTPException(status_code=500, detail=f"Failed to start update: {exc}")
    return {
        "ok": True,
        "pid": proc.pid,
        "name": "hermes-update",
    }


def _recent_upstream_commits(n: int = 20) -> List[Dict[str, Any]]:
    """Commits the local checkout is behind ``origin/main`` by, newest first.

    Logs the SAME range the behind-count uses (``HEAD..origin/main`` — see
    ``banner._check_via_local_git``), NOT the branch's ``@{upstream}``. On a
    feature-branch checkout ``@{upstream}`` is the branch's own tip (zero
    commits), which would leave the changelog empty even though the count is
    non-zero. Pinning to ``origin/main`` keeps count and changelog consistent.

    Best-effort: returns [] if not a git checkout, origin/main is unreachable,
    or git is unavailable. Never raises into the request path.
    """
    try:
        out = subprocess.run(
            [
                "git",
                "-C",
                str(PROJECT_ROOT),
                "log",
                "--format=%H%x1f%s%x1f%an%x1f%ct",
                "HEAD..origin/main",
                f"-n{int(n)}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0:
            return []
        rows: List[Dict[str, Any]] = []
        for line in out.stdout.splitlines():
            if not line.strip():
                continue
            parts = (line.split("\x1f") + ["", "", "", "0"])[:4]
            sha, summary, author, at = parts
            rows.append(
                {
                    "sha": sha[:7],
                    "summary": summary,
                    "author": author,
                    "at": int(at or 0),
                }
            )
        return rows
    except Exception:
        return []


@app.get("/api/hermes/update/check")
async def check_hermes_update(force: bool = False):
    """Report whether a Hermes update is available, without applying it.

    Powers the dashboard's "check before you update" flow: the System page
    shows the commit-behind count and asks the user to confirm before
    ``POST /api/hermes/update`` actually runs ``hermes update``.

    Returns:
        install_method: 'git' | 'pip' | 'docker' | 'nixos' | 'homebrew' | ...
        current_version: installed Hermes version string
        behind: commits behind upstream (>=1), 0 if up to date,
                -1 if behind by an unknown count (nix/pypi), or null if the
                check could not run (offline, no remote, etc.)
        update_available: convenience bool (behind is non-zero and not null)
        can_apply: True when the dashboard's update button can apply it
                   in place (git/pip); False for docker/nix/homebrew where the
                   user must update out-of-band
        update_command: the recommended command for this install method
        message: human-readable guidance for non-applyable methods
        commits: for git/pip installs that are behind, a list of the commits
                 the local checkout is behind upstream by — each
                 {sha, summary, author, at}. Absent/empty otherwise. The
                 desktop's remote update overlay renders this as "what's
                 changed". Additive: existing consumers ignore it.
    """
    if _dashboard_local_update_managed_externally():
        return {
            "install_method": "managed-runtime",
            "current_version": __version__,
            "behind": None,
            "update_available": False,
            "can_apply": False,
            "update_command": "managed outside dashboard",
            "message": (
                "Hermes updates are managed outside this dashboard in "
                "containerized environments."
            ),
        }

    install_method = detect_install_method(PROJECT_ROOT)
    update_command = recommended_update_command_for_method(install_method)

    payload: Dict[str, Any] = {
        "install_method": install_method,
        "current_version": __version__,
        "behind": None,
        "update_available": False,
        "can_apply": install_method in ("git", "pip"),
        "update_command": update_command,
        "message": None,
    }

    if install_method == "docker":
        payload["message"] = format_docker_update_message()
        return payload

    # banner.check_for_updates() handles git / pypi / nix-revision paths and
    # caches the result for 6h. ``force`` busts the cache so the "Check now"
    # button reflects reality immediately.
    try:
        from hermes_cli.banner import check_for_updates

        if force:
            try:
                (get_hermes_home() / ".update_check").unlink()
            except OSError:
                pass

        behind = await asyncio.to_thread(check_for_updates)
    except Exception:
        _log.exception("Update check failed")
        behind = None

    payload["behind"] = behind
    if behind is None:
        payload["message"] = "Couldn't reach the update source — try again later."
    elif behind == 0:
        payload["message"] = "You're on the latest version."
    else:
        payload["update_available"] = True
        # Enrich with the actual commits we're behind by, so the desktop's
        # remote update overlay can show "what's changed". git/pip only;
        # best-effort (empty list on any failure).
        if install_method in ("git", "pip"):
            payload["commits"] = await asyncio.to_thread(_recent_upstream_commits)

    return payload


@app.post("/api/audio/transcribe")
async def transcribe_audio_upload(payload: AudioTranscriptionRequest):
    data_url = (payload.data_url or "").strip()
    if not data_url.startswith("data:") or "," not in data_url:
        raise HTTPException(status_code=400, detail="Invalid audio payload")

    header, encoded = data_url.split(",", 1)
    if ";base64" not in header:
        raise HTTPException(
            status_code=400, detail="Audio payload must be base64 encoded"
        )

    mime_type = (
        payload.mime_type or header[5:].split(";", 1)[0] or "audio/webm"
    ).strip()
    normalized_mime_type = mime_type.split(";", 1)[0].lower()
    if not (
        normalized_mime_type.startswith("audio/")
        or normalized_mime_type == "video/webm"
    ):
        raise HTTPException(
            status_code=400, detail="Payload must be an audio recording"
        )

    try:
        audio_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="Audio payload is not valid base64")

    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Audio recording is empty")
    if len(audio_bytes) > _MAX_TRANSCRIPTION_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Audio recording is too large")

    temp_path = ""
    try:
        suffix = _audio_extension_for_mime(mime_type)
        with tempfile.NamedTemporaryFile(
            prefix="hermes-desktop-voice-",
            suffix=suffix,
            delete=False,
        ) as tmp:
            tmp.write(audio_bytes)
            temp_path = tmp.name

        from tools.transcription_tools import transcribe_audio

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, transcribe_audio, temp_path)
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("Desktop voice transcription failed")
        raise HTTPException(status_code=500, detail=f"Transcription failed: {exc}")
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error") or "Transcription failed",
        )

    return {
        "ok": True,
        "transcript": str(result.get("transcript") or "").strip(),
        "provider": result.get("provider"),
    }


class TTSSpeakRequest(BaseModel):
    text: str


def _elevenlabs_voice_label(voice: Dict[str, Any]) -> str:
    name = str(voice.get("name") or voice.get("voice_id") or "Voice").strip()
    category = str(voice.get("category") or "").strip()

    return f"{name} ({category})" if category else name


# Collapses repeated identical ElevenLabs voice-list failures (the desktop
# re-polls on every settings open/focus) to a single log line. Re-arms on
# success or when the error signature changes, so a real new failure is seen.
_voice_list_last_error: Optional[str] = None


def _voice_list_error_logged_once(signature: Optional[str]) -> bool:
    """Return True if ``signature`` is new and should be logged now.

    Passing ``None`` clears the latch (call on success). Idempotent per
    signature: the same error logs once until it changes.
    """
    global _voice_list_last_error
    if signature is None:
        _voice_list_last_error = None
        return False
    if signature == _voice_list_last_error:
        return False
    _voice_list_last_error = signature
    return True


@app.get("/api/audio/elevenlabs/voices")
async def get_elevenlabs_voices():
    """Return ElevenLabs voices when an API key is configured.

    The desktop UI uses this for the ``tts.elevenlabs.voice_id`` dropdown.
    Only non-secret voice metadata is returned; the API key stays server-side.
    """
    api_key = (load_env().get("ELEVENLABS_API_KEY") or os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        return {"available": False, "voices": []}

    request = urllib.request.Request(
        "https://api.elevenlabs.io/v1/voices",
        headers={
            "Accept": "application/json",
            "xi-api-key": api_key,
        },
    )

    try:
        loop = asyncio.get_running_loop()

        def _fetch() -> Dict[str, Any]:
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))

        payload = await loop.run_in_executor(None, _fetch)
    except urllib.error.HTTPError as exc:
        # An auth failure (bad/expired/scoped key) is a persistent,
        # user-fixable state, not a transient blip — the desktop polls this on
        # every settings open/focus, so a per-poll WARNING floods the log
        # (#voice-list-401-spam). Treat 401/403 as "integration unavailable":
        # report it to the UI with a 200 and log at most once until the error
        # signature changes (see _voice_list_error_logged_once).
        if exc.code in (401, 403):
            if _voice_list_error_logged_once(f"http-{exc.code}"):
                _log.info(
                    "ElevenLabs voices unavailable: %s — check ELEVENLABS_API_KEY", exc
                )
            return {"available": False, "voices": [], "error": "unauthorized"}
        if _voice_list_error_logged_once(f"http-{exc.code}"):
            _log.warning("ElevenLabs voice list failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not load ElevenLabs voices")
    except Exception as exc:
        if _voice_list_error_logged_once(str(exc)):
            _log.warning("ElevenLabs voice list failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not load ElevenLabs voices")
    _voice_list_error_logged_once(None)  # success — re-arm logging for next failure

    voices = []
    for voice in payload.get("voices") or []:
        if not isinstance(voice, dict):
            continue

        voice_id = str(voice.get("voice_id") or "").strip()
        if not voice_id:
            continue

        voices.append({
            "voice_id": voice_id,
            "name": str(voice.get("name") or voice_id),
            "label": _elevenlabs_voice_label(voice),
        })

    voices.sort(key=lambda item: str(item.get("label") or "").lower())
    return {"available": True, "voices": voices}


@app.post("/api/audio/speak")
async def speak_text(payload: TTSSpeakRequest):
    """Synthesize speech and return audio as base64 data URL.

    Used by the desktop voice-conversation mode to play back assistant
    responses without exposing the on-disk file path. Reuses the
    existing TTS provider chain (Edge / OpenAI / ElevenLabs / etc.)
    configured in ``~/.hermes/config.yaml`` under ``tts.``.
    """
    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is required")

    try:
        from tools.tts_tool import text_to_speech_tool
        loop = asyncio.get_running_loop()
        result_json = await loop.run_in_executor(None, text_to_speech_tool, text)
    except Exception as exc:
        _log.exception("Desktop voice TTS failed")
        raise HTTPException(status_code=500, detail=f"Speech synthesis failed: {exc}")

    try:
        result = json.loads(result_json) if isinstance(result_json, str) else result_json
    except Exception:
        raise HTTPException(status_code=500, detail="Invalid TTS response")

    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error") or "Speech synthesis failed",
        )

    file_path = result.get("file_path")
    if not file_path or not os.path.isfile(file_path):
        raise HTTPException(status_code=500, detail="Audio file missing")

    ext = os.path.splitext(file_path)[1].lower()
    mime_type = {
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
    }.get(ext, "audio/mpeg")

    try:
        with open(file_path, "rb") as fh:
            audio_bytes = fh.read()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Could not read audio: {exc}")
    finally:
        try:
            os.unlink(file_path)
        except OSError:
            pass

    encoded = base64.b64encode(audio_bytes).decode("ascii")
    return {
        "ok": True,
        "data_url": f"data:{mime_type};base64,{encoded}",
        "mime_type": mime_type,
        "provider": result.get("provider"),
    }


@app.get("/api/actions/{name}/status")
async def get_action_status(name: str, lines: int = 200):
    """Tail an action log and report whether the process is still running."""
    log_file_name = _ACTION_LOG_FILES.get(name)
    if log_file_name is None:
        raise HTTPException(status_code=404, detail=f"Unknown action: {name}")

    log_path = _ACTION_LOG_DIR / log_file_name
    tail = _tail_lines(log_path, min(max(lines, 1), 2000))

    proc = _ACTION_PROCS.get(name)
    if proc is None:
        result = _ACTION_RESULTS.get(name)
        running = False
        exit_code = result.get("exit_code") if result else None
        pid = result.get("pid") if result else None
    else:
        exit_code = proc.poll()
        running = exit_code is None
        pid = proc.pid
        if exit_code is not None:
            try:
                proc.wait(timeout=1)
            except Exception:
                pass
            _ACTION_RESULTS[name] = {"exit_code": exit_code, "pid": pid}
            _ACTION_PROCS.pop(name, None)
            _ACTION_COMMANDS.pop(name, None)

    return {
        "name": name,
        "running": running,
        "exit_code": exit_code,
        "pid": pid,
        "lines": tail,
    }


@app.get("/api/sessions")
async def get_sessions(
    request: Request,
    limit: int = 20,
    offset: int = 0,
    min_messages: int = 0,
    archived: str = "exclude",
    order: str = "created",
    source: str = None,
    exclude_sources: str = None,
    cwd_prefix: str = None,
    active_from: float = None,
    active_before: float = None,
    profile: Optional[str] = None,
    compact: bool = False,
):
    """List sessions.

    ``archived`` controls how soft-archived sessions are treated:
    ``exclude`` (default) hides them, ``only`` returns just the archived ones
    (used by the desktop "Archived sessions" settings panel), and ``include``
    returns both.

    ``order`` controls pagination order: ``created`` (default, by original
    start time) or ``recent`` (by latest activity across the compression
    chain). ``recent`` keeps a long-running conversation on the first page
    after it auto-compresses into a fresh continuation id.
    """
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_session_reader_http(request)
    profile_name: Optional[str] = None
    if profile:
        profile_name, _ = _cron_profile_home(profile)
    try:
        db = _open_session_db_for_profile(profile)
        try:
            return session_api.list_sessions_payload(
                db,
                limit=limit,
                offset=offset,
                min_messages=min_messages,
                archived=archived,
                order=order,
                source=source,
                exclude_sources=exclude_sources,
                cwd_prefix=cwd_prefix,
                active_from=active_from,
                active_before=active_before,
                profile_name=profile_name,
                compact=compact,
                latency_trace_id=request.headers.get("x-request-id", ""),
            )
        finally:
            db.close()
    except (HTTPException, session_api.HTTPException):
        raise
    except Exception:
        _log.exception("GET /api/sessions failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/sessions/composition")
async def get_session_composition(
    request: Request,
    ids: List[str] = Query(default=[]),
    profile: Optional[str] = None,
):
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_session_reader_http(request)
    db = _open_session_db_for_profile(profile)
    try:
        return session_api.session_composition_payload(db, ids=ids)
    finally:
        db.close()


@app.get("/api/sessions/search")
async def search_sessions(request: Request, q: str = "", limit: int = 20, profile: Optional[str] = None):
    """Search sessions by ID plus full-text message content using FTS5.

    Direct session-id matches are surfaced first, then FTS message-content
    matches. Results are deduped by compression lineage, not by raw
    ``session_id``. Auto-compression rotates a conversation onto a fresh
    session id (and leaves the old segment's messages in the FTS index), so one
    logical chat can own many ``sessions`` rows that all match the same query.
    Branches also use ``parent_session_id``, but they are real alternate
    conversations; don't collapse branch-specific hits back into the parent.
    """
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_session_reader_http(request)
    try:
        db = _open_session_db_for_profile(profile)
        try:
            return session_api.search_sessions_payload(db, q=q, limit=limit)
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception:
        _log.exception("GET /api/sessions/search failed")
        raise HTTPException(status_code=500, detail="Search failed")


def _normalize_config_for_web(config: Dict[str, Any]) -> Dict[str, Any]:
    from hermes_cli.dashboard_owner_payloads import normalize_config_for_web

    return normalize_config_for_web(config)


def _memory_provider_config_path(provider: MemoryProvider) -> Path:
    return get_hermes_home() / provider.name / "config.json"


def _read_memory_provider_file(provider: MemoryProvider) -> Dict[str, Any]:
    path = _memory_provider_config_path(provider)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        _log.warning("Failed to read memory provider config from %s", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def _read_field_value(field: ProviderField, data: Dict[str, Any]) -> str:
    """Resolve the stored value for a non-secret field, honoring legacy reads."""

    for source_key in (field.key, *field.aliases):
        value = data.get(source_key)
        if value:
            return str(value)

    env_on_disk = load_env()
    for env_key in field.env_fallbacks:
        value = env_on_disk.get(env_key)
        if value:
            return str(value)

    return field.default


def _field_is_set(field: ProviderField, data: Dict[str, Any]) -> bool:
    """Whether a secret field has a value anywhere it may have been written."""

    env_on_disk = load_env()
    for env_key in (field.env_key, *field.env_fallbacks):
        if env_key and env_on_disk.get(env_key):
            return True
    return any(data.get(source_key) for source_key in (field.key, *field.aliases))


def _memory_provider_payload(provider: MemoryProvider) -> Dict[str, Any]:
    data = _read_memory_provider_file(provider)
    fields: List[Dict[str, Any]] = []

    for field in provider.fields:
        entry: Dict[str, Any] = {
            "key": field.key,
            "label": field.label,
            "kind": field.kind,
            "description": field.description,
            "placeholder": field.placeholder,
            "options": [
                {"value": opt.value, "label": opt.label, "description": opt.description}
                for opt in field.options
            ],
        }

        if field.is_secret:
            # Secrets are write-only over the API; only expose whether one is set.
            entry["value"] = ""
            entry["is_set"] = _field_is_set(field, data)
        else:
            value = _read_field_value(field, data)
            if field.kind == "select" and value not in field.allowed_values():
                value = field.default
            entry["value"] = value
            entry["is_set"] = bool(value)

        fields.append(entry)

    return {"name": provider.name, "label": provider.label, "fields": fields}


def _coerce_field_value(field: ProviderField, raw: str) -> str:
    """Validate and normalize a submitted non-secret value, or raise ValueError."""

    value = (raw or "").strip()
    if field.kind == "select":
        if not value:
            value = field.default
        if value not in field.allowed_values():
            raise ValueError(f"Invalid value for '{field.key}'")
        return value
    return value or field.default


@app.get("/api/memory/providers/{name}/config")
async def get_memory_provider_config(name: str):
    provider = get_memory_provider(name)
    if provider is None:
        # Undeclared providers (e.g. builtin) have no config surface. Return an
        # empty schema so the generic panel simply renders nothing.
        return {"name": name, "label": name, "fields": []}
    return _memory_provider_payload(provider)


@app.put("/api/memory/providers/{name}/config")
async def update_memory_provider_config(name: str, body: MemoryProviderConfigUpdate):
    provider = get_memory_provider(name)
    if provider is None:
        raise HTTPException(status_code=404, detail=f"Unknown memory provider: {name}")

    values = body.values or {}

    try:
        existing = _read_memory_provider_file(provider)
        json_values: Dict[str, Any] = {}
        secrets: Dict[str, str] = {}

        for field in provider.fields:
            if field.is_secret:
                submitted = (values.get(field.key) or "").strip()
                if submitted and field.env_key:
                    secrets[field.env_key] = submitted
                continue

            raw = (
                values[field.key]
                if field.key in values
                else str(existing.get(field.key, field.default))
            )
            json_values[field.key] = _coerce_field_value(field, raw)

        config = load_config()
        memory_config = config.get("memory")
        if not isinstance(memory_config, dict):
            memory_config = {}
            config["memory"] = memory_config
        memory_config["provider"] = provider.name
        save_config(config)

        path = _memory_provider_config_path(provider)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing.update(json_values)
        from utils import atomic_json_write

        atomic_json_write(path, existing, mode=0o600)

        for env_key, secret in secrets.items():
            save_env_value(env_key, secret)

        return {"ok": True}
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        _log.exception("PUT /api/memory/providers/%s/config failed", name)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/config")
async def get_config(request: Request, profile: Optional[str] = None):
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_owner_http(request)
    with _profile_scope(profile):
        return _normalize_config_for_web(load_config())


@app.get("/api/config/defaults")
async def get_defaults():
    return DEFAULT_CONFIG


@app.get("/api/config/schema")
async def get_schema():
    return {"fields": CONFIG_SCHEMA, "category_order": _CATEGORY_ORDER}


@app.get("/api/model/info")
async def get_model_info(request: Request, profile: Optional[str] = None):
    """Return resolved model metadata for the currently configured model.

    Authenticated owner mode proxies this owner-sensitive config read to the
    Owner Worker.  Local/loopback mode keeps the legacy Control Plane read.
    """
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_owner_http(request)
    try:
        from hermes_cli.model_info_payload import EMPTY_MODEL_INFO, model_info_payload_from_config

        with _profile_scope(profile):
            cfg = load_config()
        return model_info_payload_from_config(cfg)
    except HTTPException:
        # Unknown/invalid profile must surface as 404, not degrade into a
        # 200 with empty model info (which would render as "no model set").
        raise
    except Exception:
        _log.exception("GET /api/model/info failed")
        return dict(EMPTY_MODEL_INFO)


def _model_registration_http_error(exc: Exception) -> HTTPException:
    from hermes_cli.model_registrations import (
        ModelRegistrationConflict,
        ModelRegistrationImmutable,
        ModelRegistrationNotFound,
    )

    if isinstance(exc, ModelRegistrationNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ModelRegistrationImmutable):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ModelRegistrationConflict):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="Internal server error")


@app.get("/api/model/registrations")
async def get_model_registrations(request: Request, profile: Optional[str] = None):
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_owner_http(request)
    from hermes_cli.model_registrations import get_model_registrations_payload

    def _load():
        with _profile_scope(profile):
            return get_model_registrations_payload()

    try:
        return await asyncio.to_thread(_load)
    except HTTPException:
        raise
    except Exception as exc:
        raise _model_registration_http_error(exc) from exc


@app.get("/api/model/registrations/catalog")
async def get_model_registration_catalog_route(
    request: Request,
    kind: str,
    profile: Optional[str] = None,
):
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_owner_http(request)
    from hermes_cli.model_registrations import get_model_registration_catalog

    def _load():
        with _profile_scope(profile):
            return get_model_registration_catalog(kind)

    try:
        return await asyncio.to_thread(_load)
    except HTTPException:
        raise
    except Exception as exc:
        raise _model_registration_http_error(exc) from exc


@app.post("/api/model/registrations")
async def create_model_registration_route(
    request: Request,
    body: ModelRegistrationPayload,
    profile: Optional[str] = None,
):
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(body.profile)
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_owner_http(request)
    from hermes_cli.model_registrations import create_model_registration

    def _create():
        with _profile_scope(body.profile or profile):
            return create_model_registration(body.dict(exclude={"id", "profile"}))

    try:
        return await asyncio.to_thread(_create)
    except HTTPException:
        raise
    except Exception as exc:
        raise _model_registration_http_error(exc) from exc


@app.put("/api/model/registrations")
async def update_model_registration_route(
    request: Request,
    body: ModelRegistrationPayload,
    profile: Optional[str] = None,
):
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(body.profile)
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_owner_http(request)
    from hermes_cli.model_registrations import update_model_registration

    def _update():
        with _profile_scope(body.profile or profile):
            return update_model_registration(
                body.id,
                body.dict(exclude={"id", "profile"}),
            )

    try:
        return await asyncio.to_thread(_update)
    except HTTPException:
        raise
    except Exception as exc:
        raise _model_registration_http_error(exc) from exc


@app.delete("/api/model/registrations")
async def delete_model_registration_route(
    request: Request,
    body: ModelRegistrationMutation,
    profile: Optional[str] = None,
):
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(body.profile)
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_owner_http(request)
    from hermes_cli.model_registrations import delete_model_registration

    def _delete():
        with _profile_scope(body.profile or profile):
            return delete_model_registration(body.id)

    try:
        return await asyncio.to_thread(_delete)
    except HTTPException:
        raise
    except Exception as exc:
        raise _model_registration_http_error(exc) from exc


@app.put("/api/model/registrations/active")
async def activate_model_registration_route(
    request: Request,
    body: ModelRegistrationMutation,
    profile: Optional[str] = None,
):
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(body.profile)
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_owner_http(request)
    from hermes_cli.model_registrations import activate_model_registration

    def _activate():
        with _profile_scope(body.profile or profile):
            return activate_model_registration(body.id)

    try:
        return await asyncio.to_thread(_activate)
    except HTTPException:
        raise
    except Exception as exc:
        raise _model_registration_http_error(exc) from exc


# ---------------------------------------------------------------------------
# Model assignment — pick provider+model for main slot or auxiliary slots.
# Mirrors the model.options JSON-RPC from tui_gateway but uses REST so the
# Models page (which has no chat PTY open) can drive it.
# ---------------------------------------------------------------------------

# Canonical auxiliary task slots. Keep in sync with DEFAULT_CONFIG["auxiliary"]
# in hermes_cli/config.py — listed here for deterministic ordering in the UI.
_AUX_TASK_SLOTS: Tuple[str, ...] = (
    "vision",
    "web_extract",
    "compression",
    "skills_hub",
    "approval",
    "mcp",
    "title_generation",
    "triage_specifier",
    "kanban_decomposer",
    "profile_describer",
    "curator",
)


@app.get("/api/model/options")
def get_model_options(profile: Optional[str] = None, refresh: bool = False):
    """Return authenticated providers + their curated model lists.

    REST equivalent of the ``model.options`` JSON-RPC on tui_gateway, so the
    dashboard Models page can render the picker without a live chat session.
    The response shape matches ``model.options`` 1:1 so ``ModelPickerDialog``
    can share the same types.

    ``profile`` scopes the picker context (current model/provider, custom
    providers from config, per-profile .env auth state) so the Models page
    reads the SAME profile /api/model/set writes.

    ``refresh`` busts the per-provider model-id disk cache so every row
    re-fetches its live catalog — used by the picker's explicit "Refresh
    Models" control. Normal opens leave it false to stay on the 1h cache.
    """
    try:
        from hermes_cli.inventory import build_models_payload, load_picker_context

        # include_unconfigured + picker_hints + canonical_order mirror the
        # tui_gateway `model.options` JSON-RPC handler exactly, so every GUI
        # surface fed by this endpoint (Settings → Model, the first-run
        # onboarding picker) sees the SAME full provider universe `hermes model`
        # exposes — not just the authenticated subset. Unconfigured providers
        # come back as skeleton rows carrying `authenticated=False` +
        # `auth_type`/`key_env`/`warning` so the GUI can render a setup
        # affordance instead of hiding the provider entirely.
        with _profile_scope(profile):
            return build_models_payload(
                load_picker_context(),
                include_unconfigured=True,
                picker_hints=True,
                canonical_order=True,
                pricing=True,
                capabilities=True,
                refresh=bool(refresh),
            )
    except HTTPException:
        raise
    except Exception:
        _log.exception("GET /api/model/options failed")
        raise HTTPException(status_code=500, detail="Failed to list model options")


@app.get("/api/model/recommended-default")
def get_recommended_default_model(provider: str = ""):
    """Return the recommended default model for a freshly-authenticated provider.

    Mirrors the model-curation `hermes model` does so GUI onboarding lands on a
    sensible default instead of blindly taking the first curated entry. For
    Nous this honors the user's free/paid tier: free users get a free model,
    paid users get the full curated default. For any other provider it falls
    back to the first curated model (same as before).

    Response: {"provider": str, "model": str, "free_tier": bool | None}
    where free_tier is True/False for Nous and None otherwise. `model` may be
    empty if nothing could be resolved (caller degrades gracefully).
    """
    slug = (provider or "").strip().lower()

    if slug == "nous":
        try:
            from hermes_cli.models import (
                get_curated_nous_model_ids,
                get_pricing_for_provider,
                check_nous_free_tier,
                partition_nous_models_by_tier,
                union_with_portal_free_recommendations,
                union_with_portal_paid_recommendations,
            )
            from hermes_cli.auth import get_provider_auth_state

            model_ids = get_curated_nous_model_ids()
            pricing = get_pricing_for_provider("nous") or {}
            free_tier = check_nous_free_tier(force_fresh=True)

            portal_url = ""
            try:
                state = get_provider_auth_state("nous") or {}
                portal_url = state.get("portal_base_url", "") or ""
            except Exception:
                portal_url = ""

            if free_tier:
                model_ids, pricing = union_with_portal_free_recommendations(
                    model_ids, pricing, portal_url
                )
                model_ids, _unavailable = partition_nous_models_by_tier(
                    model_ids, pricing, free_tier=True
                )
            else:
                model_ids, pricing = union_with_portal_paid_recommendations(
                    model_ids, pricing, portal_url
                )

            model = model_ids[0] if model_ids else ""
            return {"provider": "nous", "model": model, "free_tier": bool(free_tier)}
        except Exception:
            _log.exception("GET /api/model/recommended-default (nous) failed")
            return {"provider": "nous", "model": "", "free_tier": None}

    # Non-Nous: first curated model for the provider, matching prior behaviour.
    try:
        from hermes_cli.inventory import build_models_payload, load_picker_context

        payload = build_models_payload(load_picker_context())
        for row in payload.get("providers", []):
            if str(row.get("slug", "")).lower() == slug:
                models = row.get("models") or []
                return {"provider": slug, "model": models[0] if models else "", "free_tier": None}
        return {"provider": slug, "model": "", "free_tier": None}
    except Exception:
        _log.exception("GET /api/model/recommended-default failed")
        return {"provider": slug, "model": "", "free_tier": None}


@app.get("/api/model/auxiliary")
def get_auxiliary_models(profile: Optional[str] = None):
    """Return current auxiliary task assignments.

    Shape:
      {
        "tasks": [
          {"task": "vision", "provider": "auto", "model": "", "base_url": ""},
          ...
        ],
        "main": {"provider": "openrouter", "model": "anthropic/claude-opus-4.7"},
      }

    ``profile`` scopes the read — without it, the Models page would show
    the dashboard profile's auxiliary pins while /api/model/set wrote the
    selected profile's (read/write asymmetry).
    """
    try:
        with _profile_scope(profile):
            cfg = load_config()
        aux_cfg = cfg.get("auxiliary", {})
        if not isinstance(aux_cfg, dict):
            aux_cfg = {}

        tasks = []
        for slot in _AUX_TASK_SLOTS:
            slot_cfg = aux_cfg.get(slot, {}) if isinstance(aux_cfg.get(slot), dict) else {}
            tasks.append({
                "task": slot,
                "provider": str(slot_cfg.get("provider", "auto") or "auto"),
                "model": str(slot_cfg.get("model", "") or ""),
                "base_url": str(slot_cfg.get("base_url", "") or ""),
            })

        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, dict):
            main = {
                "provider": str(model_cfg.get("provider", "") or ""),
                "model": str(model_cfg.get("default", model_cfg.get("name", "")) or ""),
            }
        else:
            main = {"provider": "", "model": str(model_cfg) if model_cfg else ""}

        return {"tasks": tasks, "main": main}
    except HTTPException:
        raise
    except Exception:
        _log.exception("GET /api/model/auxiliary failed")
        raise HTTPException(status_code=500, detail="Failed to read auxiliary config")


@app.get("/api/model/moa")
def get_moa_models(profile: Optional[str] = None):
    """Return the configured Mixture-of-Agents provider/model slots."""
    try:
        from hermes_cli.moa_config import normalize_moa_config

        with _profile_scope(profile):
            cfg = load_config()
            return normalize_moa_config(cfg.get("moa") if isinstance(cfg, dict) else {})
    except HTTPException:
        raise
    except Exception:
        _log.exception("GET /api/model/moa failed")
        raise HTTPException(status_code=500, detail="Failed to read MoA config")


@app.put("/api/model/moa")
def set_moa_models(body: MoaConfigPayload, profile: Optional[str] = None):
    """Persist the Mixture-of-Agents provider/model slots."""
    try:
        from hermes_cli.moa_config import normalize_moa_config

        with _profile_scope(body.profile or profile):
            cfg = load_config()
            if body.presets:
                raw = {
                    "default_preset": body.default_preset,
                    "active_preset": body.active_preset,
                    "presets": {
                        name: {
                            "reference_models": [slot.dict() for slot in preset.reference_models],
                            "aggregator": preset.aggregator.dict(),
                            "reference_temperature": preset.reference_temperature,
                            "aggregator_temperature": preset.aggregator_temperature,
                            "max_tokens": preset.max_tokens,
                            "enabled": preset.enabled,
                        }
                        for name, preset in body.presets.items()
                    },
                }
            else:
                raw = {
                    "reference_models": [slot.dict() for slot in body.reference_models],
                    "aggregator": body.aggregator.dict(),
                    "reference_temperature": body.reference_temperature,
                    "aggregator_temperature": body.aggregator_temperature,
                    "max_tokens": body.max_tokens,
                    "enabled": body.enabled,
                }
            normalized = normalize_moa_config(raw)
            cfg["moa"] = normalized
            save_config(cfg)
            return {"ok": True, **normalized}
    except HTTPException:
        raise
    except Exception:
        _log.exception("PUT /api/model/moa failed")
        raise HTTPException(status_code=500, detail="Failed to save MoA config")


@app.post("/api/model/set")
async def set_model_assignment(body: ModelAssignment, profile: Optional[str] = None):
    """Assign a model to the main slot or an auxiliary task slot.

    Writes to ``~/.hermes/config.yaml`` — applies to **new** sessions only.
    The currently running chat PTY (if any) is not affected; use the
    ``/model`` slash command inside a chat to hot-swap that specific session.
    """
    scope = (body.scope or "").strip().lower()
    provider = (body.provider or "").strip()
    model = (body.model or "").strip()
    task = (body.task or "").strip().lower()
    base_url = (body.base_url or "").strip()
    api_key = (body.api_key or "").strip()

    if scope not in {"main", "auxiliary"}:
        raise HTTPException(status_code=400, detail="scope must be 'main' or 'auxiliary'")

    try:
        # Expensive-model warning runs BEFORE the profile scope is entered:
        # _profile_scope must never be held across an await (the RLock is
        # reentrant per-thread, so a second coroutine interleaving on the
        # event-loop thread could cross-restore the module globals).
        if model and not body.confirm_expensive_model:
            try:
                from hermes_cli.model_cost_guard import expensive_model_warning

                # Pricing lookup can hit models.dev / a /models endpoint on a
                # cache miss — keep it off the event loop.
                warning = await asyncio.to_thread(
                    expensive_model_warning,
                    model,
                    provider=provider,
                    base_url=base_url,
                )
            except Exception:
                warning = None
            if warning is not None:
                return {
                    "ok": False,
                    "scope": scope,
                    "provider": provider,
                    "model": model,
                    "confirm_required": True,
                    "confirm_message": warning.message,
                }

        def _apply_assignment():
            with _profile_scope(body.profile or profile):
                return _apply_model_assignment_sync(
                    scope, provider, model, task, base_url, api_key
                )

        return await asyncio.to_thread(_apply_assignment)
    except HTTPException:
        raise
    except Exception:
        _log.exception("POST /api/model/set failed")
        raise HTTPException(status_code=500, detail="Failed to save model assignment")


def _apply_model_assignment_sync(
    scope: str, provider: str, model: str, task: str, base_url: str, api_key: str = ""
):
    """Synchronous body of POST /api/model/set.

    Runs inside ``_profile_scope`` (in a worker thread) so every
    load_config/save_config lands in the requested profile.  Raises
    HTTPException for validation errors — the async wrapper re-raises them.
    """
    cfg = load_config()

    if scope == "main":
        if not provider or not model:
            raise HTTPException(status_code=400, detail="provider and model required for main")
        provider, model = _normalize_main_model_assignment(provider, model)
        model_cfg = _apply_main_model_assignment(
            cfg.get("model", {}), provider, model, base_url, api_key
        )
        cfg["model"] = model_cfg

        # When switching the main provider to Nous, mirror the CLI's
        # post-model-selection behaviour (hermes_cli/main.py
        # prompt_enable_tool_gateway / tools_config apply_nous_managed_defaults):
        # auto-route any *unconfigured* tools through the Nous Tool Gateway.
        # This is purely additive — apply_nous_managed_defaults skips every
        # tool where the user already has a direct key (FIRECRAWL_API_KEY,
        # FAL_KEY, etc.) or an explicit backend/provider in config, so it
        # never overwrites a user's own setup. GUI users thus land on the
        # gateway the same way CLI users do, without a separate prompt.
        gateway_tools: list[str] = []
        if provider.strip().lower() == "nous":
            try:
                from hermes_cli.nous_subscription import apply_nous_managed_defaults
                from hermes_cli.tools_config import _get_platform_tools

                enabled = _get_platform_tools(
                    cfg, "cli", include_default_mcp_servers=False
                )
                changed = apply_nous_managed_defaults(
                    cfg,
                    enabled_toolsets=enabled,
                    force_fresh=True,
                )
                gateway_tools = sorted(changed)
            except Exception:
                # Portal lookup hiccups / non-subscriber / non-nous gating
                # must never block saving the model assignment.
                _log.debug("apply_nous_managed_defaults skipped", exc_info=True)

        save_config(cfg)

        # Register a named ``custom_providers`` entry for a custom/local
        # endpoint, mirroring the ``hermes model`` custom flow
        # (_save_custom_provider). Without this the endpoint only lives in
        # ``model.*`` and the picker has no proper ready row for it — the
        # GUI then surfaces a "needs setup" dead-end on the bare ``custom``
        # provider. Dedups by base_url, so re-saving is idempotent.
        if provider.strip().lower() in {"custom", "local"} and base_url:
            try:
                from hermes_cli.main import _auto_provider_name, _save_custom_provider

                _save_custom_provider(
                    base_url,
                    api_key,
                    model,
                    name=_auto_provider_name(base_url),
                )
            except Exception:
                # Never block the assignment on the bookkeeping write —
                # model.* is already persisted and routable.
                _log.debug("custom_providers registration skipped", exc_info=True)

        # Surface auxiliary slots still pinned to a *different* provider than
        # the new main one. Switching the main model does NOT touch aux pins
        # (they're independent, sticky per-task overrides — see
        # auxiliary_client._resolve_auto). A user who switches main away from
        # a now-unpaid provider (e.g. nous with $0 balance) keeps paying 402s
        # on every background aux call until they reset those pins. We never
        # auto-clear them — pinning aux to a cheaper/different model is a
        # legitimate config — but we tell the caller so the UI can offer a
        # "reset to main" nudge instead of silently burning credits.
        new_provider = provider.strip().lower()
        stale_aux: list[dict] = []
        aux_cfg = cfg.get("auxiliary", {})
        if isinstance(aux_cfg, dict):
            for slot in _AUX_TASK_SLOTS:
                slot_cfg = aux_cfg.get(slot)
                if not isinstance(slot_cfg, dict):
                    continue
                slot_provider = str(slot_cfg.get("provider", "") or "").strip()
                if (
                    slot_provider
                    and slot_provider.lower() not in {"auto", ""}
                    and slot_provider.lower() != new_provider
                ):
                    stale_aux.append({
                        "task": slot,
                        "provider": slot_provider,
                        "model": str(slot_cfg.get("model", "") or ""),
                    })

        return {
            "ok": True,
            "scope": "main",
            "provider": provider,
            "model": model,
            "base_url": model_cfg.get("base_url", ""),
            "gateway_tools": gateway_tools,
            "stale_aux": stale_aux,
        }

    # scope == "auxiliary"
    aux = cfg.get("auxiliary")
    if not isinstance(aux, dict):
        aux = {}

    if task == "__reset__":
        # Reset every slot to provider="auto", model="" — keeps other fields intact.
        for slot in _AUX_TASK_SLOTS:
            slot_cfg = aux.get(slot)
            if not isinstance(slot_cfg, dict):
                slot_cfg = {}
            slot_cfg["provider"] = "auto"
            slot_cfg["model"] = ""
            slot_cfg.pop("base_url", None)
            clear_model_endpoint_credentials(slot_cfg)
            aux[slot] = slot_cfg
        cfg["auxiliary"] = aux
        save_config(cfg)
        return {"ok": True, "scope": "auxiliary", "reset": True}

    if not provider:
        raise HTTPException(status_code=400, detail="provider required for auxiliary")

    targets = [task] if task else list(_AUX_TASK_SLOTS)
    for slot in targets:
        if slot not in _AUX_TASK_SLOTS:
            raise HTTPException(status_code=400, detail=f"unknown auxiliary task: {slot}")
        slot_cfg = aux.get(slot)
        if not isinstance(slot_cfg, dict):
            slot_cfg = {}
        prev_provider = str(slot_cfg.get("provider") or "").strip().lower()
        new_provider = provider.strip().lower()
        slot_cfg["provider"] = provider
        slot_cfg["model"] = model
        if new_provider != prev_provider and new_provider != "custom":
            slot_cfg.pop("base_url", None)
            clear_model_endpoint_credentials(slot_cfg)
        aux[slot] = slot_cfg

    cfg["auxiliary"] = aux
    save_config(cfg)
    return {
        "ok": True,
        "scope": "auxiliary",
        "tasks": targets,
        "provider": provider,
        "model": model,
    }


def _infer_provider_on_model_change(model_val: str, prev_provider: str) -> tuple[str, str]:
    """Infer which provider serves ``model_val`` when the flat Config-page Model
    field changes, given the previously-saved ``prev_provider``.

    Returns ``(provider, model)``; ``provider`` is empty when no switch is
    warranted (leave the existing provider untouched). Two signals, in order:

    1. Curated-catalog detection (``detect_provider_for_model``) — handles the
       ~28 OpenRouter-curated models and direct provider-static catalogs.
    2. Vendor-slug heuristic — a ``vendor/model`` slug cannot belong to a
       single-model / non-aggregator provider (e.g. ``ollama-local``). When the
       current provider is not an aggregator that serves vendor-prefixed slugs,
       route to an aggregator. ``_normalize_main_model_assignment`` (called by
       the caller) keeps the user's current aggregator when they're already on
       one, else falls back to openrouter — the same chokepoint logic as
       ``POST /api/model/set``.
    """
    name = (model_val or "").strip()
    if not name:
        return "", name
    try:
        from hermes_cli.models import (
            _AGGREGATOR_PROVIDERS,
            detect_provider_for_model,
            normalize_provider,
        )
    except Exception:
        return "", name

    try:
        detected = detect_provider_for_model(name, prev_provider)
    except Exception:
        detected = None
    if detected:
        return detected[0], detected[1]

    # Vendor-prefixed slug under a non-aggregator provider → reassign. Use a
    # sentinel "openrouter" here; _normalize_main_model_assignment resolves the
    # real aggregator (keeps a current aggregator, else openrouter).
    if "/" in name:
        try:
            cur_is_aggregator = normalize_provider(prev_provider) in _AGGREGATOR_PROVIDERS
        except Exception:
            cur_is_aggregator = False
        if not cur_is_aggregator:
            return "openrouter", name

    return "", name


def _denormalize_config_from_web(config: Dict[str, Any]) -> Dict[str, Any]:
    """Reverse _normalize_config_for_web before saving.

    Reconstructs ``model`` as a dict by reading the current on-disk config
    to recover model subkeys (provider, base_url, api_mode, etc.) that were
    stripped from the GET response.  The frontend only sees model as a flat
    string; the rest is preserved transparently.

    Also handles ``model_context_length`` — writes it back into the model dict
    as ``context_length``.  A value of 0 or absent means "auto-detect" (omitted
    from the dict so get_model_context_length() uses its normal resolution).
    """
    config = dict(config)
    # Remove any _model_meta that might have leaked in (shouldn't happen
    # with the stripped GET response, but be defensive)
    config.pop("_model_meta", None)

    # Extract and remove model_context_length before processing model
    ctx_override = config.pop("model_context_length", 0)
    if not isinstance(ctx_override, int):
        try:
            ctx_override = int(ctx_override)
        except (TypeError, ValueError):
            ctx_override = 0

    model_val = config.get("model")
    if isinstance(model_val, str) and model_val:
        # Read the current disk config to recover model subkeys
        try:
            disk_config = load_config()
            disk_model = disk_config.get("model")
            if isinstance(disk_model, dict):
                prev_default = str(disk_model.get("default") or "").strip()
                prev_provider = str(disk_model.get("provider") or "").strip()
                # When the model name actually changed, re-detect which
                # provider serves it. The Config-page Model field is a flat
                # string with no provider info, so without this a user who
                # picks an OpenRouter model while their default provider is
                # ollama-local keeps the stale provider and 404s. Only fires
                # on a real model change so saving unrelated config fields
                # never overwrites an explicit provider.
                if model_val != prev_default and prev_provider:
                    new_provider, resolved_model = _infer_provider_on_model_change(
                        model_val, prev_provider
                    )
                    if new_provider and new_provider.strip().lower() != prev_provider.lower():
                        # Route through the canonical assignment chokepoints so
                        # the model is normalized for the new provider and stale
                        # base_url/api_mode/api_key are cleared on the switch
                        # (and preserved on a same-provider re-pick).
                        norm_provider, norm_model = _normalize_main_model_assignment(
                            new_provider, resolved_model
                        )
                        disk_model = _apply_main_model_assignment(
                            disk_model, norm_provider, norm_model
                        )
                        model_val = norm_model
                # Preserve all subkeys, update default with the new value
                disk_model["default"] = model_val
                # Write context_length into the model dict (0 = remove/auto)
                if ctx_override > 0:
                    disk_model["context_length"] = ctx_override
                else:
                    disk_model.pop("context_length", None)
                config["model"] = disk_model
            # Model was previously a bare string — upgrade to dict if
            # user is setting a context_length override
            elif ctx_override > 0:
                config["model"] = {
                    "default": model_val,
                    "context_length": ctx_override,
                }
        except Exception:
            pass  # can't read disk config — just use the string form
    return config


@app.put("/api/config")
async def update_config(body: ConfigUpdate, profile: Optional[str] = None):
    try:
        with _profile_scope(body.profile or profile):
            # The dashboard form is schema-driven (see CONFIG_SCHEMA). Any root
            # key absent from the schema — most visibly ``custom_providers``, but
            # also ``agent.personalities``, ``terminal.lifetime_seconds``, etc. —
            # is not sent in the PUT body. A full-replace save would silently
            # drop those keys. Deep-merge incoming over what's on disk so the
            # frontend can only overwrite what it explicitly sends.
            existing = read_raw_config()
            incoming = _denormalize_config_from_web(body.config)
            save_config(_deep_merge(existing, incoming))
        return {"ok": True}
    except HTTPException:
        raise
    except Exception:
        _log.exception("PUT /api/config failed")
        raise HTTPException(status_code=500, detail="Internal server error")


def _catalog_provider_env_metadata() -> dict:
    """Map provider env vars → desktop card metadata, derived from the catalog.

    Returns ``{env_var: {provider, provider_label, description, url, is_password,
    advanced}}`` for every API-key provider in the unified ``provider_catalog()``
    (i.e. the ``hermes model`` universe). This is what lets the desktop Keys tab
    render a card for a provider even when its env var was never hand-added to
    ``OPTIONAL_ENV_VARS`` — closing the drift where CLI-configurable providers
    (openai-api, kilocode, novita, tencent-tokenhub, copilot, …) were missing
    from the GUI.

    Hand ``OPTIONAL_ENV_VARS`` prose is layered ON TOP of this in the endpoint;
    this only supplies membership + grouping + sensible fallbacks.
    """
    try:
        from hermes_cli.provider_catalog import provider_catalog
    except Exception:
        return {}

    # Env vars already declared with a NON-provider category (e.g. the shared
    # GITHUB_TOKEN, which is a Skills-Hub "tool" credential) must not be
    # promoted into a provider card. Copilot lists GITHUB_TOKEN among its auth
    # aliases, but its provider card uses the provider-owned COPILOT_GITHUB_TOKEN.
    try:
        from hermes_cli.config import OPTIONAL_ENV_VARS as _OPT
    except Exception:
        _OPT = {}
    _non_provider_keys = {
        k for k, v in _OPT.items()
        if (v or {}).get("category") and (v or {}).get("category") != "provider"
    }

    meta: dict = {}
    for d in provider_catalog():
        if d.tab != "keys":
            continue
        # API-key vars: the first is the primary (password) field; any aliases
        # are kept as additional password fields so users can clear them too.
        for env_var in d.api_key_env_vars:
            if env_var in _non_provider_keys:
                continue  # don't hijack a shared tool/messaging credential
            meta.setdefault(
                env_var,
                {
                    "provider": d.slug,
                    "provider_label": d.label,
                    "description": d.description,
                    "url": d.signup_url or None,
                    "is_password": True,
                    "advanced": False,
                    "category": "provider",
                },
            )
        # Base-URL override is an advanced, non-secret field for the same card.
        if d.base_url_env_var:
            meta.setdefault(
                d.base_url_env_var,
                {
                    "provider": d.slug,
                    "provider_label": d.label,
                    "description": f"{d.label} base URL override",
                    "url": None,
                    "is_password": False,
                    "advanced": True,
                    "category": "provider",
                },
            )

        # AWS-SDK providers (Bedrock) authenticate via the AWS credential chain
        # rather than a pasted API key, so they have no api_key_env_vars. Tag
        # their AWS_* settings to the provider card so they still appear on the
        # Keys tab (otherwise Bedrock — a `hermes model` provider — would be
        # invisible in the desktop app).
        if d.auth_type == "aws_sdk":
            for aws_var in ("AWS_REGION", "AWS_PROFILE"):
                existing = meta.get(aws_var, {})
                meta[aws_var] = {
                    "provider": d.slug,
                    "provider_label": d.label,
                    "description": existing.get("description") or f"{d.label} ({aws_var})",
                    "url": existing.get("url"),
                    "is_password": False,
                    "advanced": existing.get("advanced", True),
                    "category": "provider",
                }

        # Vertex AI authenticates via OAuth2 (service-account JSON or ADC), not a
        # pasted API key, so it also has no api_key_env_vars. Tag its credential
        # env var to the provider card so it appears on the Keys tab (otherwise
        # Vertex — a `hermes model` provider — would be invisible in the desktop
        # app). The value is a filesystem path, not a secret string, so it is
        # not a password field.
        if d.auth_type == "vertex":
            existing = meta.get("VERTEX_CREDENTIALS_PATH", {})
            meta["VERTEX_CREDENTIALS_PATH"] = {
                "provider": d.slug,
                "provider_label": d.label,
                "description": existing.get("description")
                or f"{d.label} — service account JSON path (or use ADC)",
                "url": existing.get("url"),
                "is_password": False,
                "advanced": existing.get("advanced", True),
                "category": "provider",
            }
    return meta


@app.get("/api/env")
async def get_env_vars(profile: Optional[str] = None):
    with _profile_scope(profile):
        env_on_disk = load_env()
    channel_keys = _channel_managed_env_keys()
    catalog_meta = _catalog_provider_env_metadata()

    def _row(var_name: str, info: dict, *, custom: bool = False) -> dict:
        value = env_on_disk.get(var_name)
        cat_meta = catalog_meta.get(var_name) or {}
        # Hand OPTIONAL_ENV_VARS prose wins where present; the catalog fills any
        # gaps (description/url) and always supplies provider grouping hints.
        return {
            "is_set": bool(value),
            "redacted_value": redact_key(value) if value else None,
            "description": info.get("description") or cat_meta.get("description", ""),
            "url": info.get("url") if info.get("url") is not None else cat_meta.get("url"),
            "category": info.get("category") or cat_meta.get("category", ""),
            "is_password": info.get("password", cat_meta.get("is_password", False)),
            "tools": info.get("tools", []),
            "advanced": info.get("advanced", cat_meta.get("advanced", False)),
            # True when this var is a messaging-platform credential owned by a
            # Channels page card. The Keys/Env page uses this to hide it and
            # avoid duplicating the (richer) Channels configuration UI.
            "channel_managed": var_name in channel_keys,
            # Provider grouping hints derived from the unified provider catalog
            # so the desktop Keys tab groups by the SAME provider identity the
            # CLI `hermes model` picker uses (not desktop-only prefix guesses).
            "provider": cat_meta.get("provider", ""),
            "provider_label": cat_meta.get("provider_label", ""),
            # True when this key exists in the user's .env but is NOT in any
            # catalog (OPTIONAL_ENV_VARS or the provider catalog) — an
            # arbitrary/custom env var the user added directly. Surfaced so the
            # Keys page can list (and let the user manage) them instead of
            # hiding everything it doesn't recognise.
            "custom": custom,
        }

    result = {}
    for var_name, info in OPTIONAL_ENV_VARS.items():
        result[var_name] = _row(var_name, info)
    # Synthesize rows for catalog provider env vars that have no hand entry in
    # OPTIONAL_ENV_VARS — these are the providers that were CLI-configurable but
    # invisible in the desktop app until now.
    for var_name in catalog_meta:
        if var_name not in result:
            result[var_name] = _row(var_name, {})
    # Surface arbitrary/custom keys the user set in .env that aren't in any
    # catalog. These are always "set" (they're on disk). Treated as secrets by
    # default (is_password=True → redacted, reveal-gated) since an unrecognised
    # key could hold anything. Channel-managed credentials are excluded — those
    # belong to the Channels page. This makes the "add a custom key" surface
    # round-trip: a key added there reappears here under its own section.
    for var_name in env_on_disk:
        if var_name in result or var_name in channel_keys:
            continue
        row = _row(var_name, {}, custom=True)
        row["category"] = "custom"
        row["is_password"] = True
        result[var_name] = row
    return result


@app.put("/api/env")
async def set_env_var(body: EnvVarUpdate, profile: Optional[str] = None):
    try:
        with _profile_scope(body.profile or profile):
            save_env_value(body.key, body.value)
        return {"ok": True, "key": body.key}
    except ValueError as exc:
        # save_env_value raises ValueError for invalid names and for keys
        # on the denylist (LD_PRELOAD, PATH, PYTHONPATH, …). Surface the
        # message to the SPA so the user understands why the write was
        # refused instead of seeing an opaque 500.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        _log.exception("PUT /api/env failed")
        raise HTTPException(status_code=500, detail="Internal server error")


# Live credential probes keyed by env var. Each entry is (method, url, auth)
# where auth is "bearer" (Authorization header) or "query" (?key=). A cheap
# read-only models/key call that 401s on a bad token — enough to catch a
# mistyped key before it's persisted. Providers absent from this map (or local
# endpoints) are not network-validated; the client treats those as "unknown".
_CREDENTIAL_PROBES: dict[str, tuple[str, str]] = {
    "OPENROUTER_API_KEY": ("https://openrouter.ai/api/v1/key", "bearer"),
    "OPENAI_API_KEY": ("https://api.openai.com/v1/models", "bearer"),
    "XAI_API_KEY": ("https://api.x.ai/v1/models", "bearer"),
    "GEMINI_API_KEY": ("https://generativelanguage.googleapis.com/v1beta/models", "query"),
}


def _parse_model_ids(resp: "Any") -> List[str]:
    """Extract model ids from an OpenAI-compatible ``/v1/models`` response.

    Tolerant of the common shapes: ``{"data": [{"id": ...}]}`` (OpenAI / vLLM /
    llama.cpp) and a bare ``{"data": ["id", ...]}``. Returns ``[]`` on any
    parse/HTTP error so a slightly non-standard endpoint never hard-blocks.
    """
    try:
        if not resp.is_success:
            return []
        payload = resp.json()
    except Exception:
        return []
    data = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(data, list):
        return []
    ids: List[str] = []
    for item in data:
        if isinstance(item, dict):
            mid = str(item.get("id") or "").strip()
        else:
            mid = str(item or "").strip()
        if mid:
            ids.append(mid)
    return ids


@app.post("/api/providers/validate")
async def validate_provider_credential(body: EnvVarUpdate, request: Request):
    """Live-probe a provider credential before it's saved.

    Returns {ok, reachable, message}. ok=True means the provider accepted the
    key; ok=False + reachable=True means the key is bad (caller should block);
    reachable=False means the network probe couldn't run (caller may save with
    a warning rather than hard-blocking offline users).
    """
    import httpx

    key = (body.key or "").strip()
    value = (body.value or "").strip()
    if not value:
        return {"ok": False, "reachable": True, "message": "Enter a value first."}

    # Local / custom endpoint: validate connectivity, not auth — any HTTP
    # response (even 401) proves the endpoint is up. Also surface the model
    # ids the endpoint advertises (OpenAI ``/v1/models`` shape) so the GUI can
    # auto-pick a default without asking the user to type a model name.
    if key == "OPENAI_BASE_URL":
        url = value.rstrip("/") + "/models"
        # Send the optional API key so endpoints that require auth on
        # ``/v1/models`` (many hosted OpenAI-compatible servers) still enumerate
        # their models instead of returning an empty list behind a 401.
        api_key = (body.api_key or "").strip()
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        try:
            with httpx.Client(timeout=httpx.Timeout(8.0)) as client:
                resp = client.get(url, headers=headers)
            return {"ok": True, "reachable": True, "message": "", "models": _parse_model_ids(resp)}
        except Exception:
            return {"ok": False, "reachable": False, "message": f"Could not reach {url}."}

    probe = _CREDENTIAL_PROBES.get(key)
    if not probe:
        # No probe for this provider — can't validate, don't block.
        return {"ok": True, "reachable": False, "message": ""}

    url, auth = probe
    headers = {"Accept": "application/json"}
    params = {}
    if auth == "bearer":
        headers["Authorization"] = f"Bearer {value}"
    else:
        params["key"] = value

    try:
        with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
            resp = client.get(url, headers=headers, params=params)
    except Exception:
        return {"ok": False, "reachable": False, "message": "Could not reach the provider to verify the key."}

    if resp.status_code in (401, 403):
        return {"ok": False, "reachable": True, "message": "That API key was rejected. Double-check it and try again."}
    if resp.status_code == 429 or resp.is_success:
        # 429 = key is valid but rate-limited; success = valid.
        return {"ok": True, "reachable": True, "message": ""}
    return {"ok": False, "reachable": True, "message": f"Provider returned HTTP {resp.status_code} for this key."}


@app.delete("/api/env")
async def remove_env_var(body: EnvVarDelete, profile: Optional[str] = None):
    try:
        with _profile_scope(body.profile or profile):
            removed = remove_env_value(body.key)
        if not removed:
            raise HTTPException(status_code=404, detail=f"{body.key} not found in .env")
        return {"ok": True, "key": body.key}
    except HTTPException:
        raise
    except ValueError as exc:
        # remove_env_value raises ValueError for invalid key names. Surface
        # the message to the SPA so the user understands why the delete was
        # refused instead of seeing an opaque 500. Mirrors PUT /api/env.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:
        _log.exception("DELETE /api/env failed")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/env/reveal")
async def reveal_env_var(
    body: EnvVarReveal, request: Request, profile: Optional[str] = None
):
    """Return the real (unredacted) value of a single env var.

    Protected by:
    - Ephemeral session token (generated per server start, injected into SPA)
    - Rate limiting (max 5 reveals per 30s window)
    - Audit logging
    """
    # --- Token check ---

    # --- Rate limit ---
    now = time.time()
    cutoff = now - _REVEAL_WINDOW_SECONDS
    _reveal_timestamps[:] = [t for t in _reveal_timestamps if t > cutoff]
    if len(_reveal_timestamps) >= _REVEAL_MAX_PER_WINDOW:
        raise HTTPException(status_code=429, detail="Too many reveal requests. Try again shortly.")
    _reveal_timestamps.append(now)

    # --- Reveal ---
    with _profile_scope(body.profile or profile):
        env_on_disk = load_env()
    value = env_on_disk.get(body.key)
    if value is None:
        raise HTTPException(status_code=404, detail=f"{body.key} not found in .env")

    _log.info("env/reveal: %s", body.key)
    return {"key": body.key, "value": value}


# Canonical connector metadata exposed by the dashboard backend.
_PLATFORM_OVERRIDES: dict[str, dict[str, Any]] = {
    "weixin_ilink": {
        "name": "Weixin iLink",
        "description": "Canonical Weixin iLink connector.",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/user-guide/messaging/weixin-ilink",
        "env_vars": (),
    },
    "feishu": {
        "name": "Feishu",
        "description": "Canonical Feishu connector.",
        "docs_url": "",
        "env_vars": (),
    },
    "webhook": {
        "name": "Webhook",
        "description": "Authenticated Owner-bound Webhook connector.",
        "docs_url": "",
        "env_vars": (),
    },
}

_PLATFORM_ORDER: tuple[str, ...] = ("weixin_ilink", "feishu", "webhook")


def _messaging_platform_catalog() -> tuple[dict[str, Any], ...]:
    """Return dashboard metadata for canonical messaging connectors only."""
    return tuple(
        {"id": provider, **_PLATFORM_OVERRIDES[provider]}
        for provider in _PLATFORM_ORDER
    )


def _channel_managed_env_keys() -> frozenset[str]:
    """Env-var keys owned by a Channels page platform card.

    The Channels page is the canonical surface for configuring messaging
    platform credentials (with connection status, test, enable toggle and
    gateway restart). The Keys/Env page consults this set to hide those vars
    so the same fields aren't duplicated in a plainer UI. Best-effort: if the
    gateway catalog can't be built, nothing is flagged and Keys shows it all.
    """
    try:
        keys: set[str] = set()
        for entry in _messaging_platform_catalog():
            keys.update(entry.get("env_vars", ()))
        return frozenset(keys)
    except Exception:
        _log.debug("could not build channel-managed env key set", exc_info=True)
        return frozenset()


def _catalog_lookup(platform_id: str) -> dict[str, Any] | None:
    for entry in _messaging_platform_catalog():
        if entry["id"] == platform_id:
            return entry
    return None


class WebhookConnectorCreate(BaseModel):
    response_url: str
    prompt_template: str = "{payload}"
    allowed_events: list[str] = []


def _employee_authority_context(request: Request):
    if not bool(getattr(request.app.state, "auth_required", False)):
        raise HTTPException(status_code=403, detail="authentication_required")
    session = getattr(request.state, "session", None)
    if session is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    runtime = getattr(request.app.state, "channel_connector_runtime", None)
    store = getattr(runtime, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="connector_runtime_unavailable")
    from hermes_cli.dashboard_auth.owner_context import owner_context_from_session

    return runtime, store, owner_context_from_session(session)


@app.post("/api/messaging/webhook/accounts", status_code=201)
async def create_webhook_connector_account(
    request: Request,
    body: WebhookConnectorCreate,
):
    """Provision one authenticated Webhook account for the signed-in Owner."""
    if not bool(getattr(request.app.state, "auth_required", False)):
        raise HTTPException(status_code=403, detail="authentication_required")
    session = getattr(request.state, "session", None)
    if session is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    runtime = getattr(request.app.state, "channel_connector_runtime", None)
    store = getattr(runtime, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="connector_runtime_unavailable")

    from hermes_cli.channel_connectors.webhook import _validate_callback_url
    from hermes_cli.dashboard_auth.owner_context import owner_context_from_session

    try:
        response_url = _validate_callback_url(body.response_url)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    route_token = secrets.token_urlsafe(32)
    hmac_secret = secrets.token_urlsafe(48)
    response_hmac_secret = secrets.token_urlsafe(48)
    credentials = {
        "hmac_secret": hmac_secret,
        "prompt_template": body.prompt_template,
        "allowed_events": body.allowed_events,
        "response_url": response_url,
        "response_hmac_secret": response_hmac_secret,
    }
    registered = register_connector_binding_for_owner(
        store,
        owner=owner_context_from_session(session),
        provider="webhook",
        provider_account_id=route_token,
        external_subject=route_token,
        conversation_id=route_token,
        credentials=credentials,
    )
    return {
        "account_id": registered.account_id,
        "binding_id": registered.binding_id,
        "route_token": route_token,
        "webhook_path": f"/webhooks/{route_token}",
        "hmac_secret": hmac_secret,
        "response_hmac_secret": response_hmac_secret,
    }


def _employee_avatar_url(store, employee_id: str) -> str | None:
    from hermes_cli.channel_identity.employee_avatars import employee_avatar_version

    version = employee_avatar_version(store, employee_id)
    return f"/api/employees/{employee_id}/avatar?v={version}" if version is not None else None


def _employee_payload(runtime, store, owner, employee) -> dict[str, Any]:
    from hermes_cli.channel_identity import (
        builtin_assistant_personalization_payload,
        resolve_builtin_assistant_personalization,
        resolve_employee_profile,
    )

    if employee.employee_kind == "builtin_assistant":
        profile = resolve_builtin_assistant_personalization(
            store,
            owner=owner,
            employee_id=employee.employee_id,
        )
        public_profile = None
        personalization = builtin_assistant_personalization_payload(profile)
    else:
        profile = (
            resolve_employee_profile(
                store,
                owner=owner,
                employee_id=employee.employee_id,
                revision=employee.profile_revision,
            )
            if employee.profile_revision is not None
            else None
        )
        public_profile = profile.profile if profile is not None else None
        personalization = None
    avatar_url = _employee_avatar_url(store, employee.employee_id)
    channels: dict[str, Any] = {}
    binding = employee.feishu_binding
    if binding is not None:
        states = getattr(getattr(runtime, "status", None), "states", {})
        runtime_state = (
            states.get(f"feishu:{binding.connector_account_id}")
            if isinstance(states, dict)
            else None
        )
        channels["feishu"] = {
            "binding_id": binding.binding_id,
            "connector_account_id": binding.connector_account_id,
            "app_id": binding.provider_account_id,
            "credential_version": binding.credential_version,
            "lifecycle_status": binding.lifecycle_status,
            "runtime_state": runtime_state or "stopped",
        }
    return {
        "employee_id": employee.employee_id,
        "employee_kind": employee.employee_kind,
        "protected": employee.protected,
        "chat_eligible": employee.chat_eligible,
        "avatar_url": avatar_url,
        "lifecycle_status": employee.lifecycle_status,
        "profile_revision": profile.revision if profile is not None else None,
        "profile_fingerprint": profile.fingerprint if profile is not None else None,
        "profile": public_profile,
        **(
            {"builtin_assistant_personalization": personalization}
            if personalization is not None
            else {}
        ),
        "collaboration_policy": {
            "may_participate": employee.collaboration_policy.may_participate,
            "may_create_groups": employee.collaboration_policy.may_create_groups,
            "invite_quota": employee.collaboration_policy.invite_quota,
        },
        "channels": channels,
    }


def _employee_or_404(store, owner, employee_id: str):
    from hermes_cli.channel_identity import resolve_employee

    try:
        return resolve_employee(store, owner=owner, employee_id=employee_id)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="Employee not found") from exc


def _reject_builtin_employee_http(employee) -> None:
    from hermes_cli.channel_identity import (
        BuiltinEmployeeProtected,
        reject_builtin_employee_mutation,
    )

    try:
        reject_builtin_employee_mutation(employee)
    except BuiltinEmployeeProtected as exc:
        raise HTTPException(
            status_code=409, detail="builtin_employee_protected"
        ) from exc


def _feishu_binding_or_404(
    store, owner, employee_id: str, *, include_revoked: bool = False
):
    from hermes_cli.channel_identity import resolve_employee_feishu_binding

    try:
        return resolve_employee_feishu_binding(
            store,
            owner=owner,
            employee_id=employee_id,
            include_revoked=include_revoked,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail="Feishu binding not found") from exc


@app.get("/api/employees/catalog")
async def get_employee_catalog(request: Request):
    if _authenticated_owner_request(request):
        _employee_authority_context(request)
        return await _proxy_authenticated_owner_http(request)
    from hermes_cli.employee_catalog import employee_catalog_payload

    owner_home = Path(os.environ.get("HERMES_OWNER_HOME") or Path.home())
    return employee_catalog_payload(owner_home)


_EMPLOYEE_LIST_STATUSES = frozenset({"active", "suspended", "revoked"})
_EMPLOYEE_LIST_DEFAULT_PAGE_SIZE = 50
_EMPLOYEE_LIST_MAX_PAGE_SIZE = 200


def _employee_list_page_params(request: Request) -> tuple[int, int]:
    try:
        page = int(request.query_params.get("page", "1"))
        page_size = int(
            request.query_params.get(
                "page_size", str(_EMPLOYEE_LIST_DEFAULT_PAGE_SIZE)
            )
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="employee list pagination is invalid"
        ) from exc
    if page < 1 or not 1 <= page_size <= _EMPLOYEE_LIST_MAX_PAGE_SIZE:
        raise HTTPException(
            status_code=400, detail="employee list pagination is invalid"
        )
    return page, page_size


def _employee_matches_keyword(payload: dict[str, Any], keyword: str) -> bool:
    profile = payload.get("profile") or {}
    haystack = f"{profile.get('name') or ''}\n{profile.get('role') or ''}".lower()
    return keyword in haystack


@app.get("/api/employees")
async def list_employee_records(request: Request):
    runtime, store, owner = _employee_authority_context(request)
    from hermes_cli.channel_identity import list_employees

    status = str(request.query_params.get("status") or "").strip().lower()
    if status and status not in _EMPLOYEE_LIST_STATUSES:
        raise HTTPException(status_code=400, detail="employee status is invalid")
    keyword = str(request.query_params.get("query") or "").strip().lower()
    page, page_size = _employee_list_page_params(request)

    payloads: list[dict[str, Any]] = []
    for employee in list_employees(store, owner=owner):
        if status:
            if employee.lifecycle_status != status:
                continue
        elif employee.lifecycle_status == "revoked":
            # Deleted employees stay hidden unless explicitly requested.
            continue
        payload = _employee_payload(runtime, store, owner, employee)
        if keyword and not _employee_matches_keyword(payload, keyword):
            continue
        payloads.append(payload)
    total = len(payloads)
    start = (page - 1) * page_size
    return {
        "employees": payloads[start : start + page_size],
        "page": page,
        "page_size": page_size,
        "total": total,
    }


@app.get("/api/employees/{employee_id}")
async def get_employee_record(request: Request, employee_id: str):
    runtime, store, owner = _employee_authority_context(request)
    employee = _employee_or_404(store, owner, employee_id)
    return _employee_payload(runtime, store, owner, employee)


@app.post("/api/employees", status_code=201)
async def create_employee_record(request: Request, body: EmployeeCreate):
    runtime, store, owner = _employee_authority_context(request)
    from hermes_cli.channel_identity import create_employee

    try:
        employee = create_employee(
            store,
            owner=owner,
            profile=body.profile,
            activate=body.activate,
        )
        return _employee_payload(runtime, store, owner, employee)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/employees/{employee_id}/avatar")
async def get_employee_avatar(request: Request, employee_id: str):
    _runtime, store, owner = _employee_authority_context(request)
    _employee_or_404(store, owner, employee_id)
    from hermes_cli.channel_identity.employee_avatars import employee_avatar_path

    target = employee_avatar_path(store, employee_id)
    if target.is_symlink() or not target.is_file():
        raise HTTPException(status_code=404, detail="Employee avatar not found")
    return FileResponse(
        target,
        media_type="image/webp",
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-cache"},
    )


@app.put("/api/employees/{employee_id}/avatar")
async def update_employee_avatar(request: Request, employee_id: str):
    _runtime, store, owner = _employee_authority_context(request)
    _employee_or_404(store, owner, employee_id)
    from hermes_cli.channel_identity.employee_avatars import (
        MAX_AVATAR_UPLOAD_BYTES,
        EmployeeAvatarInvalid,
        save_employee_avatar,
    )

    form = await request.form()
    file = form.get("file")
    if not callable(getattr(file, "read", None)) or not callable(getattr(file, "close", None)):
        raise HTTPException(status_code=422, detail="Invalid avatar upload")
    data = bytearray()
    try:
        while chunk := await file.read(1024 * 1024):
            data.extend(chunk)
            if len(data) > MAX_AVATAR_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="Employee avatar is too large")
    finally:
        await file.close()
    try:
        save_employee_avatar(store, employee_id, bytes(data))
    except EmployeeAvatarInvalid as exc:
        raise HTTPException(status_code=400, detail="Employee avatar is invalid") from exc
    return {"avatar_url": _employee_avatar_url(store, employee_id)}


@app.delete("/api/employees/{employee_id}/avatar")
async def delete_employee_avatar_route(request: Request, employee_id: str):
    _runtime, store, owner = _employee_authority_context(request)
    _employee_or_404(store, owner, employee_id)
    from hermes_cli.channel_identity.employee_avatars import delete_employee_avatar

    return {"ok": True, "deleted": delete_employee_avatar(store, employee_id)}


@app.put("/api/employees/{employee_id}/builtin-assistant-personalization")
async def update_builtin_assistant_personalization_route(
    request: Request,
    employee_id: str,
    body: BuiltinAssistantPersonalizationUpdate,
):
    runtime, store, owner = _employee_authority_context(request)
    employee = _employee_or_404(store, owner, employee_id)
    if employee.employee_kind != "builtin_assistant":
        raise HTTPException(status_code=404, detail="Employee not found")
    from hermes_cli.channel_identity import (
        EmployeeProfileRevisionConflict,
        update_builtin_assistant_personalization,
    )

    try:
        update_builtin_assistant_personalization(
            store,
            owner=owner,
            employee_id=employee_id,
            nickname=body.nickname,
            personal_preference=body.personal_preference,
            expected_revision=body.expected_revision,
        )
        return _employee_payload(runtime, store, owner, employee)
    except EmployeeProfileRevisionConflict as exc:
        raise HTTPException(status_code=409, detail="employee_profile_revision_conflict") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/employees/{employee_id}/profile")
async def update_employee_profile_route(
    request: Request,
    employee_id: str,
    body: EmployeeProfileUpdate,
):
    runtime, store, owner = _employee_authority_context(request)
    employee = _employee_or_404(store, owner, employee_id)
    _reject_builtin_employee_http(employee)
    from hermes_cli.channel_identity import EmployeeProfileRevisionConflict, update_employee_profile

    try:
        update_employee_profile(
            store,
            owner=owner,
            employee_id=employee_id,
            profile=body.profile,
            expected_revision=body.expected_revision,
        )
        return _employee_payload(
            runtime, store, owner, _employee_or_404(store, owner, employee_id)
        )
    except EmployeeProfileRevisionConflict as exc:
        raise HTTPException(status_code=409, detail="employee_profile_revision_conflict") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/employees/{employee_id}/collaboration-policy")
async def update_employee_collaboration_policy_route(
    request: Request,
    employee_id: str,
    body: EmployeeCollaborationPolicyUpdate,
):
    runtime, store, owner = _employee_authority_context(request)
    employee = _employee_or_404(store, owner, employee_id)
    _reject_builtin_employee_http(employee)
    from hermes_cli.channel_identity import update_employee_collaboration_policy

    try:
        update_employee_collaboration_policy(
            store,
            owner=owner,
            employee_id=employee_id,
            may_participate=body.may_participate,
            may_create_groups=body.may_create_groups,
            invite_quota=body.invite_quota,
        )
        return _employee_payload(
            runtime, store, owner, _employee_or_404(store, owner, employee_id)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/api/employees/{employee_id}/lifecycle")
async def update_employee_lifecycle(
    request: Request,
    employee_id: str,
    body: EmployeeLifecycleUpdate,
):
    runtime, store, owner = _employee_authority_context(request)
    employee = _employee_or_404(store, owner, employee_id)
    _reject_builtin_employee_http(employee)
    from hermes_cli.channel_identity import set_employee_status

    status = str(body.status or "").strip().lower()
    try:
        employee = set_employee_status(
            store, owner=owner, employee_id=employee_id, status=status
        )
        return _employee_payload(runtime, store, owner, employee)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        if "cannot be reactivated" in str(exc):
            raise HTTPException(status_code=409, detail="employee_revoked") from exc
        raise HTTPException(status_code=503, detail="employee_lifecycle_update_failed") from exc


@app.post("/api/employees/{employee_id}/rollover")
async def rollover_employee_session_bindings(request: Request, employee_id: str):
    _runtime, store, owner = _employee_authority_context(request)
    employee = _employee_or_404(store, owner, employee_id)
    _reject_builtin_employee_http(employee)
    from hermes_cli.channel_identity import rollover_employee_sessions

    try:
        retired = rollover_employee_sessions(
            store, owner=owner, employee_id=employee_id
        )
        return {"ok": True, "retired_sessions": retired}
    except RuntimeError as exc:
        if "active conversations" in str(exc):
            raise HTTPException(status_code=409, detail="employee_conversations_active") from exc
        raise


@app.put("/api/employees/{employee_id}/channels/feishu", status_code=201)
async def create_employee_feishu_binding(
    request: Request,
    employee_id: str,
    body: FeishuBindingCreate,
):
    runtime, store, owner = _employee_authority_context(request)
    employee = _employee_or_404(store, owner, employee_id)
    _reject_builtin_employee_http(employee)
    from hermes_cli.channel_connectors.feishu import verify_feishu_credentials
    from hermes_cli.channel_identity import register_employee_feishu_binding

    try:
        verified = await verify_feishu_credentials({
            "app_id": body.app_id,
            "app_secret": body.app_secret,
            "domain": body.domain,
            "encrypt_key": body.encrypt_key,
            "verification_token": body.verification_token,
        })
        credentials = {
            **verified,
            "app_secret": body.app_secret,
            "encrypt_key": body.encrypt_key,
            "verification_token": body.verification_token,
        }
        binding = register_employee_feishu_binding(
            store,
            owner=owner,
            employee_id=employee_id,
            provider_account_id=verified["app_id"],
            credentials=credentials,
            activate=body.activate,
        )
        if body.activate:
            runtime.register_feishu_account(binding.connector_account_id)
            try:
                await runtime.start_account("feishu", binding.connector_account_id)
            except Exception as exc:
                _log.warning(
                    "employee Feishu startup failed employee_id=%s error_type=%s",
                    employee_id,
                    type(exc).__name__,
                )
        return _employee_payload(
            runtime, store, owner, _employee_or_404(store, owner, employee_id)
        )
    except HTTPException:
        raise
    except Exception as exc:
        _log.warning(
            "employee Feishu binding creation failed employee_id=%s error_type=%s",
            employee_id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=400, detail="feishu_binding_setup_failed") from exc


@app.put("/api/employees/{employee_id}/channels/feishu/credentials")
async def rotate_employee_feishu_binding_credentials(
    request: Request,
    employee_id: str,
    body: FeishuBindingCredentialRotate,
):
    runtime, store, owner = _employee_authority_context(request)
    employee = _employee_or_404(store, owner, employee_id)
    _reject_builtin_employee_http(employee)
    binding = _feishu_binding_or_404(store, owner, employee_id)
    from hermes_cli.channel_connectors.feishu import verify_feishu_credentials
    from hermes_cli.channel_identity import (
        FeishuCredentialRevisionConflict,
        resolve_employee_feishu_credentials,
        rotate_employee_feishu_credentials,
    )

    current, _ = resolve_employee_feishu_credentials(
        store, owner=owner, employee_id=employee_id
    )
    candidate = {
        "app_id": binding.provider_account_id,
        "app_secret": body.app_secret,
        "domain": str(current.get("domain") or "feishu"),
        "encrypt_key": (
            body.encrypt_key
            if body.encrypt_key is not None
            else str(current.get("encrypt_key") or "")
        ),
        "verification_token": (
            body.verification_token
            if body.verification_token is not None
            else str(current.get("verification_token") or "")
        ),
    }
    try:
        verified = await verify_feishu_credentials(candidate)
        current_identity = {
            str(current.get(key) or "").strip()
            for key in ("bot_open_id", "bot_user_id", "bot_union_id")
            if str(current.get(key) or "").strip()
        }
        next_identity = {
            str(verified.get(key) or "").strip()
            for key in ("bot_open_id", "bot_user_id", "bot_union_id")
            if str(verified.get(key) or "").strip()
        }
        if not current_identity or current_identity.isdisjoint(next_identity):
            raise HTTPException(status_code=409, detail="feishu_bot_identity_changed")
        updated = rotate_employee_feishu_credentials(
            store,
            owner=owner,
            employee_id=employee_id,
            credentials={**candidate, **verified},
            expected_credential_version=body.expected_credential_version,
        )
        if updated.lifecycle_status == "active":
            await runtime.stop_account("feishu", updated.connector_account_id)
            try:
                await runtime.start_account("feishu", updated.connector_account_id)
            except Exception:
                return JSONResponse(
                    status_code=202,
                    content={
                        **_employee_payload(
                            runtime,
                            store,
                            owner,
                            _employee_or_404(store, owner, employee_id),
                        ),
                        "operation_state": "credentials_saved_startup_failed",
                    },
                )
        return _employee_payload(
            runtime, store, owner, _employee_or_404(store, owner, employee_id)
        )
    except FeishuCredentialRevisionConflict as exc:
        raise HTTPException(status_code=409, detail="feishu_credential_revision_conflict") from exc
    except HTTPException:
        raise
    except Exception as exc:
        _log.warning(
            "employee Feishu credential rotation failed employee_id=%s error_type=%s",
            employee_id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=400, detail="feishu_credential_rotation_failed") from exc


@app.post("/api/employees/{employee_id}/channels/feishu/test")
async def test_employee_feishu_binding(request: Request, employee_id: str):
    _runtime, store, owner = _employee_authority_context(request)
    employee = _employee_or_404(store, owner, employee_id)
    _reject_builtin_employee_http(employee)
    _feishu_binding_or_404(store, owner, employee_id)
    from hermes_cli.channel_connectors.feishu import verify_feishu_credentials
    from hermes_cli.channel_identity import resolve_employee_feishu_credentials

    credentials, _ = resolve_employee_feishu_credentials(
        store, owner=owner, employee_id=employee_id
    )
    try:
        identity = await verify_feishu_credentials(credentials)
        return {"ok": True, "state": "connected", "bot_name": identity.get("bot_name") or None}
    except Exception:
        return {"ok": False, "state": "connection_failed", "error_code": "feishu_connection_failed"}


@app.put("/api/employees/{employee_id}/channels/feishu/lifecycle")
async def update_employee_feishu_binding_lifecycle(
    request: Request,
    employee_id: str,
    body: FeishuBindingLifecycleUpdate,
):
    runtime, store, owner = _employee_authority_context(request)
    employee = _employee_or_404(store, owner, employee_id)
    _reject_builtin_employee_http(employee)
    status = str(body.status or "").strip().lower()
    binding = _feishu_binding_or_404(
        store, owner, employee_id, include_revoked=status == "active"
    )
    from hermes_cli.channel_identity import set_employee_feishu_binding_status

    try:
        if status in {"suspended", "revoked"}:
            await runtime.stop_account("feishu", binding.connector_account_id)
        updated = set_employee_feishu_binding_status(
            store, owner=owner, employee_id=employee_id, status=status
        )
        startup_failed = False
        if status == "active":
            if updated.connector_account_id not in runtime.connectors.accounts("feishu"):
                runtime.register_feishu_account(updated.connector_account_id)
            try:
                await runtime.start_account("feishu", updated.connector_account_id)
            except Exception:
                startup_failed = True
        payload = _employee_payload(
            runtime, store, owner, _employee_or_404(store, owner, employee_id)
        )
        if startup_failed:
            return JSONResponse(
                status_code=202,
                content={
                    **payload,
                    "operation_state": "lifecycle_saved_startup_failed",
                },
            )
        return payload
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        if "cannot be reactivated" in str(exc):
            raise HTTPException(status_code=409, detail="feishu_binding_revoked") from exc
        raise HTTPException(status_code=503, detail="feishu_lifecycle_update_failed") from exc
    except Exception as exc:
        _log.warning(
            "employee Feishu lifecycle update failed employee_id=%s error_type=%s",
            employee_id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=503, detail="feishu_lifecycle_update_failed") from exc


def _connector_runtime_states() -> dict[str, str]:
    runtime = getattr(app.state, "channel_connector_runtime", None)
    status = getattr(runtime, "status", None)
    states = getattr(status, "states", None)
    return states if isinstance(states, dict) else {}


def _messaging_platform_payload(
    entry: dict[str, Any],
    connector_states: dict[str, str] | None = None,
) -> dict[str, Any]:
    platform_id = entry["id"]
    try:
        connectors = load_config().get("channel_connectors") or {}
        connector_config = connectors.get(platform_id) or {}
        enabled = bool(
            isinstance(connector_config, dict)
            and connector_config.get("enabled", True)
        )
    except Exception:
        enabled = False

    runtime_state = (connector_states or {}).get(platform_id)
    if not enabled:
        state = "disabled"
    elif runtime_state == "ready":
        state = "connected"
    else:
        state = runtime_state or "pending_restart"

    configured = state not in {
        "account_unavailable",
        "authenticated_dashboard_required",
        "control_plane_unavailable",
        "deployment_policy_unavailable",
        "resource_governance_unavailable",
        "startup_failed",
        "unsupported",
    }
    error_state = state if state in {
        "control_plane_unavailable",
        "deployment_policy_unavailable",
        "resource_governance_unavailable",
        "startup_failed",
        "unsupported",
    } else None

    return {
        "id": platform_id,
        "name": entry["name"],
        "description": entry["description"],
        "docs_url": entry["docs_url"],
        "enabled": enabled,
        "configured": configured,
        "gateway_running": bool(connector_states),
        "state": state,
        "error_code": error_state,
        "error_message": None,
        "updated_at": None,
        "home_channel": None,
        "env_vars": [],
    }


def _write_platform_enabled(platform_id: str, enabled: bool) -> None:
    write_channel_connector_config_field(platform_id, "enabled", enabled)


@app.get("/api/messaging/platforms")
async def get_messaging_platforms(profile: Optional[str] = None):
    with _profile_scope(profile):
        connector_states = _connector_runtime_states() if profile is None else {}
        return {
            "env_path": str(get_env_path()),
            "gateway_start_command": _gateway_display_command(profile, "start"),
            "platforms": [
                _messaging_platform_payload(entry, connector_states)
                for entry in _messaging_platform_catalog()
            ],
        }


@app.put("/api/messaging/platforms/{platform_id}")
async def update_messaging_platform(
    platform_id: str, body: MessagingPlatformUpdate, profile: Optional[str] = None
):
    entry = _catalog_lookup(platform_id)
    if not entry:
        raise HTTPException(
            status_code=404, detail=f"Unknown messaging platform: {platform_id}"
        )

    allowed_env = set(entry["env_vars"])
    try:
        with _profile_scope(body.profile or profile):
            for key in body.clear_env:
                if key not in allowed_env:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{key} is not configurable for {entry['name']}",
                    )
                remove_env_value(key)

            for key, value in body.env.items():
                if key not in allowed_env:
                    raise HTTPException(
                        status_code=400,
                        detail=f"{key} is not configurable for {entry['name']}",
                    )
                trimmed = value.strip()
                if trimmed:
                    _validate_messaging_env_value(platform_id, key, trimmed)
                    save_env_value(key, trimmed)

            if body.enabled is not None:
                _write_platform_enabled(platform_id, body.enabled)

        return {"ok": True, "platform": platform_id}
    except HTTPException:
        raise
    except Exception:
        _log.exception("PUT /api/messaging/platforms/%s failed", platform_id)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/api/messaging/platforms/{platform_id}/test")
async def test_messaging_platform(platform_id: str, profile: Optional[str] = None):
    entry = _catalog_lookup(platform_id)
    if not entry:
        raise HTTPException(
            status_code=404, detail=f"Unknown messaging platform: {platform_id}"
        )

    with _profile_scope(profile):
        connector_states = _connector_runtime_states() if profile is None else {}
        payload = _messaging_platform_payload(entry, connector_states)
    if not payload["enabled"]:
        message = f"{entry['name']} is disabled. Enable it, then restart the gateway."
        return {"ok": False, "state": payload["state"], "message": message}
    if not payload["configured"]:
        missing = [
            field["key"]
            for field in payload["env_vars"]
            if field["required"] and not field["is_set"]
        ]
        message = (
            f"Missing required setup: {', '.join(missing)}"
            if missing
            else "Platform setup is incomplete."
        )
        return {"ok": False, "state": payload["state"], "message": message}
    if not payload["gateway_running"]:
        return {
            "ok": False,
            "state": payload["state"],
            "message": "Gateway is not running. Restart the gateway to connect this platform.",
        }
    if payload["state"] == "connected":
        return {
            "ok": True,
            "state": payload["state"],
            "message": f"{entry['name']} is connected.",
        }
    if payload.get("error_message"):
        return {
            "ok": False,
            "state": payload["state"],
            "message": payload["error_message"],
        }
    return {
        "ok": False,
        "state": payload["state"],
        "message": "Setup looks complete, but the gateway has not reported a connection yet. Restart the gateway.",
    }


# ---------------------------------------------------------------------------
# OAuth provider endpoints — status + disconnect (Phase 1)
# ---------------------------------------------------------------------------
#
# Phase 1 surfaces *which OAuth providers exist* and whether each is
# connected, plus a disconnect button. The actual login flow (PKCE for
# Anthropic, device-code for Nous/Codex) still runs in the CLI for now;
# Phase 2 will add in-browser flows. For unconnected providers we return
# the canonical ``hermes auth add <provider>`` command so the dashboard
# can surface a one-click copy.


def _truncate_token(value: Optional[str], visible: int = 6) -> str:
    """Return ``...XXXXXX`` (last N chars) for safe display in the UI.

    We never expose more than the trailing ``visible`` characters of an
    OAuth access token. JWT prefixes (the part before the first dot) are
    stripped first when present so the visible suffix is always part of
    the signing region rather than a meaningless header chunk.

    Returns the Entra-ID placeholder when handed a callable (Azure Foundry
    bearer provider) — the callable is NEVER invoked here.
    """
    if not value:
        return ""
    if callable(value) and not isinstance(value, str):
        # Entra ID bearer provider — never reveal a minted token in the UI.
        return "<entra-id-bearer>"
    s = str(value)
    if "." in s and s.count(".") >= 2:
        # Looks like a JWT — show the trailing piece of the signature only.
        s = s.rsplit(".", 1)[-1]
    if len(s) <= visible:
        return s
    return f"…{s[-visible:]}"


def _anthropic_oauth_status() -> Dict[str, Any]:
    """Status for the "Anthropic API Key" catalog entry.

    Two sources, in priority order:
    1. ``~/.hermes/.anthropic_oauth.json`` — Hermes-managed PKCE flow (what
       this entry's Connect button writes)
    2. ``ANTHROPIC_API_KEY`` → ``ANTHROPIC_TOKEN`` → ``CLAUDE_CODE_OAUTH_TOKEN``
       env vars (registry order) — from ``.env``, the shell, or an external
       secret source like Bitwarden (whose keys are injected into the process
       env during ``load_hermes_dotenv()``, so the same check covers them)

    Claude Code's ``~/.claude/.credentials.json`` is deliberately NOT read
    here — it has its own dedicated catalog entry (``claude-code`` →
    ``_claude_code_only_status``). Reporting it under the API-key entry
    double-counts the token and shadows a real ANTHROPIC_API_KEY.
    """
    try:
        from agent.anthropic_adapter import (
            read_hermes_oauth_credentials,
            _HERMES_OAUTH_FILE,
        )
    except ImportError:
        read_hermes_oauth_credentials = None  # type: ignore
        _HERMES_OAUTH_FILE = None  # type: ignore

    hermes_creds = None
    if read_hermes_oauth_credentials:
        try:
            hermes_creds = read_hermes_oauth_credentials()
        except Exception:
            hermes_creds = None
    if hermes_creds and hermes_creds.get("accessToken"):
        return {
            "logged_in": True,
            "source": "hermes_pkce",
            "source_label": f"Hermes PKCE ({_HERMES_OAUTH_FILE})",
            "token_preview": _truncate_token(hermes_creds.get("accessToken")),
            "expires_at": hermes_creds.get("expiresAt"),
            "has_refresh_token": bool(hermes_creds.get("refreshToken")),
        }

    # Env-var / secret-source path. ``get_env_value`` checks the process
    # environment first (where Bitwarden-sourced secrets land) then .env.
    env_var_order: tuple = ("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
    try:
        from hermes_cli.auth import PROVIDER_REGISTRY
        env_var_order = PROVIDER_REGISTRY["anthropic"].api_key_env_vars
    except (ImportError, KeyError):
        pass
    try:
        from hermes_cli.config import get_env_value
    except ImportError:
        get_env_value = None  # type: ignore
    try:
        from hermes_cli.env_loader import format_secret_source_suffix
    except ImportError:
        format_secret_source_suffix = None  # type: ignore

    for var in env_var_order:
        value = (get_env_value(var) if get_env_value else None) or os.getenv(var)
        if not value:
            continue
        suffix = format_secret_source_suffix(var) if format_secret_source_suffix else ""
        return {
            "logged_in": True,
            "source": "env_var",
            "source_label": f"{var}{suffix}",
            "token_preview": _truncate_token(value),
            "expires_at": None,
            "has_refresh_token": False,
        }
    return {"logged_in": False, "source": None}


def _claude_code_only_status() -> Dict[str, Any]:
    """Surface Claude Code CLI credentials as their own provider entry.

    Independent of the Anthropic entry above so users can see whether their
    Claude Code subscription tokens are actively flowing into Hermes even
    when they also have a separate Hermes-managed PKCE login.
    """
    try:
        from agent.anthropic_adapter import read_claude_code_credentials
        creds = read_claude_code_credentials()
    except Exception:
        creds = None
    if creds and creds.get("accessToken"):
        return {
            "logged_in": True,
            "source": "claude_code_cli",
            "source_label": "~/.claude/.credentials.json",
            "token_preview": _truncate_token(creds.get("accessToken")),
            "expires_at": creds.get("expiresAt"),
            "has_refresh_token": bool(creds.get("refreshToken")),
        }
    return {"logged_in": False, "source": None}


def _copilot_acp_status() -> Dict[str, Any]:
    """Status for copilot-acp — credentials are owned by the Copilot CLI.

    There is no cheap programmatic credential probe for the ACP subprocess, so
    this is a read-only "managed by the Copilot CLI" card (like claude-code):
    Hermes never claims a login state it can't verify.
    """
    return {
        "logged_in": False,
        "source": "copilot_cli",
        "source_label": "Managed by the GitHub Copilot CLI",
        "token_preview": None,
        "expires_at": None,
        "has_refresh_token": False,
    }


# Explicit, hand-tuned OAuth/account provider cards. These carry the bits that
# can't be derived from the unified provider catalog: the OAuth ``flow`` shape,
# the per-provider ``status_fn``, the ``cli_command`` fallback, and curated
# display order. They are the OVERRIDE BASE for ``_build_oauth_catalog()``,
# which unions them with every accounts-tab provider in ``provider_catalog()``
# so newly-added OAuth/external providers appear automatically (no hand edit).
# This tuple also still includes two entries that are NOT catalog providers but
# must show on the Accounts tab: the api-key Anthropic PKCE card and the
# synthetic ``claude-code`` subscription row.
# ``flow`` describes the OAuth shape so the modal can pick the right UI:
# ``pkce`` = open URL + paste callback code, ``device_code`` = show code +
# verification URL + poll, ``external`` = read-only (delegated to a third-party
# CLI like Claude Code or Qwen).
_OAUTH_PROVIDER_CATALOG: tuple[Dict[str, Any], ...] = (
    {
        "id": "nous",
        "name": "Nous Portal",
        "flow": "device_code",
        "cli_command": "hermes auth add nous",
        "docs_url": "https://portal.nousresearch.com",
        "status_fn": None,  # dispatched via auth.get_nous_auth_status
    },
    {
        "id": "openai-codex",
        "name": "OpenAI OAuth (ChatGPT)",
        "flow": "device_code",
        "cli_command": "hermes auth add openai-codex",
        "docs_url": "https://platform.openai.com/docs",
        "status_fn": None,  # dispatched via auth.get_codex_auth_status
    },
    {
        "id": "qwen-oauth",
        "name": "Qwen (via Qwen CLI)",
        "flow": "external",
        "cli_command": "hermes auth add qwen-oauth",
        "docs_url": "https://github.com/QwenLM/qwen-code",
        "status_fn": None,  # dispatched via auth.get_qwen_auth_status
    },
    {
        "id": "minimax-oauth",
        "name": "MiniMax (OAuth)",
        # MiniMax's flow is structurally device-code (verification URI +
        # user code, backend polls the token endpoint) with a PKCE
        # extension for code-binding. The dashboard renders the same UX
        # as Nous's device-code flow; the PKCE bit is a security
        # extension that doesn't change the operator experience.
        "flow": "device_code",
        "cli_command": "hermes auth add minimax-oauth",
        "docs_url": "https://www.minimax.io",
        "status_fn": None,  # dispatched via auth.get_minimax_oauth_auth_status
    },
    {
        "id": "xai-oauth",
        "name": "xAI Grok OAuth (SuperGrok / Premium+)",
        # Device code is the default because it works in remote shells,
        # containers, and desktop installs without requiring a reachable
        # 127.0.0.1 callback.
        "flow": "device_code",
        "cli_command": "hermes auth add xai-oauth",
        "docs_url": "https://hermes-agent.nousresearch.com/docs/guides/xai-grok-oauth",
        "status_fn": None,  # dispatched via auth.get_xai_oauth_auth_status
    },
    {
        "id": "copilot-acp",
        "name": "GitHub Copilot (ACP)",
        "flow": "external",
        "cli_command": "copilot /login",
        "docs_url": "https://docs.github.com/en/copilot",
        "status_fn": _copilot_acp_status,
    },
    # ── Anthropic / Claude entries sit at the bottom: the API-key path
    # first, then the subscription OAuth path (which only works with extra
    # usage credits on top of a Claude Max plan — see disclaimer in name).
    {
        "id": "anthropic",
        "name": "Anthropic API Key",
        "flow": "pkce",
        "cli_command": "hermes auth add anthropic",
        "docs_url": "https://docs.claude.com/en/api/getting-started",
        "status_fn": _anthropic_oauth_status,
    },
    {
        "id": "claude-code",
        "name": "Anthropic OAuth: Required Extra Usage Credits to Use Subscription",
        "flow": "external",
        "cli_command": "claude setup-token",
        "docs_url": "https://docs.claude.com/en/docs/claude-code",
        "status_fn": _claude_code_only_status,
    },
)


def _resolve_provider_status(provider_id: str, status_fn) -> Dict[str, Any]:
    """Dispatch to the right status helper for an OAuth provider entry."""
    if status_fn is not None:
        try:
            return status_fn()
        except Exception as e:
            return {"logged_in": False, "error": str(e)}
    try:
        from hermes_cli import auth as hauth
        if provider_id == "nous":
            raw = hauth.get_nous_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": "nous_portal",
                "source_label": raw.get("portal_base_url") or "Nous Portal",
                "token_preview": _truncate_token(raw.get("access_token")),
                "expires_at": raw.get("access_expires_at"),
                "has_refresh_token": bool(raw.get("has_refresh_token")),
            }
        if provider_id == "openai-codex":
            raw = hauth.get_codex_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": raw.get("source") or "openai_codex",
                "source_label": raw.get("auth_mode") or "OpenAI Codex",
                "token_preview": _truncate_token(raw.get("api_key")),
                "expires_at": None,
                "has_refresh_token": False,
                "last_refresh": raw.get("last_refresh"),
            }
        if provider_id == "qwen-oauth":
            raw = hauth.get_qwen_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": "qwen_cli",
                "source_label": raw.get("auth_store_path") or "Qwen CLI",
                "token_preview": _truncate_token(raw.get("access_token")),
                "expires_at": raw.get("expires_at"),
                "has_refresh_token": bool(raw.get("has_refresh_token")),
            }
        if provider_id == "minimax-oauth":
            raw = hauth.get_minimax_oauth_auth_status()
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": "minimax_oauth",
                "source_label": f"MiniMax ({raw.get('region', 'global')})",
                "token_preview": None,
                "expires_at": raw.get("expires_at"),
                "has_refresh_token": True,
            }
        if provider_id == "xai-oauth":
            raw = hauth.get_xai_oauth_auth_status()
            # source_label is meant to be a human-readable origin (auth-store
            # path / credential source), not the internal auth_mode string
            # ("oauth_pkce"). Prefer the store path, then the source slug.
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": raw.get("source") or "xai_oauth",
                "source_label": raw.get("auth_store") or raw.get("source") or "xAI Grok OAuth",
                "token_preview": _truncate_token(raw.get("api_key")),
                "expires_at": None,
                "has_refresh_token": True,
                "last_refresh": raw.get("last_refresh"),
            }
        # No hand-written branch for this provider id: fall through to the
        # canonical slug-driven dispatcher so accounts-tab providers derived
        # from the unified catalog (which carry status_fn=None) still reflect
        # real login state instead of rendering permanently logged-out. This
        # closes the membership-auto-extends-but-status-doesn't gap: add an
        # OAuth/account provider plugin and its card shows the right state.
        raw = hauth.get_auth_status(provider_id)
        if isinstance(raw, dict) and "logged_in" in raw:
            return {
                "logged_in": bool(raw.get("logged_in")),
                "source": raw.get("source") or raw.get("provider") or provider_id,
                "source_label": (
                    raw.get("source_label")
                    or raw.get("auth_store")
                    or raw.get("auth_store_path")
                    or raw.get("base_url")
                    or raw.get("name")
                    or ""
                ),
                "token_preview": _truncate_token(
                    raw.get("access_token") or raw.get("api_key")
                ),
                "expires_at": raw.get("expires_at") or raw.get("access_expires_at"),
                "has_refresh_token": bool(raw.get("has_refresh_token")),
            }
    except Exception as e:
        return {"logged_in": False, "error": str(e)}
    return {"logged_in": False}


def _oauth_provider_disconnect_command(provider: Dict[str, Any]) -> Optional[str]:
    """Shell command that clears an external provider's credentials.

    External providers store their credentials outside Hermes, so the disconnect
    API deliberately refuses them (we never delete files another CLI owns on the
    user's behalf via a silent API call). For the ones we know how to clear we
    instead hand the GUI a command it can *run in the embedded terminal* — the
    user sees exactly what executes, and Hermes then stops resolving the token.

    Claude Code has no scriptable logout (only the interactive ``/logout``), so
    we remove the credential the same way logout does: the macOS Keychain entry
    (``Claude Code-credentials``) and/or the ``~/.claude/.credentials.json``
    file — the two sources ``read_claude_code_credentials()`` consults. Returns
    None for providers we can't safely clear (the GUI shows a manual hint).
    """
    if provider.get("flow") != "external":
        return None
    if provider.get("id") == "claude-code":
        rm_file = "rm -f ~/.claude/.credentials.json"
        if sys.platform == "darwin":
            return f'security delete-generic-password -s "Claude Code-credentials" 2>/dev/null; {rm_file}'
        return rm_file
    return None


def _oauth_provider_disconnect_hint(provider: Dict[str, Any], status: Dict[str, Any]) -> Optional[str]:
    """Return the manual disconnect path when the API cannot clear this provider."""
    if provider.get("flow") == "external":
        if _oauth_provider_disconnect_command(provider):
            # The GUI offers a one-click "run in terminal" path; this hint is the
            # fallback wording for surfaces that only show text.
            return "Managed outside Hermes — run the disconnect command to remove it."
        return "Managed by that provider's CLI; remove it there."
    if status.get("source") == "env_var":
        return "Remove the API key from Settings → Keys instead."
    return None


def _build_oauth_catalog() -> list[Dict[str, Any]]:
    """Build the Accounts-tab provider list.

    MEMBERSHIP is the union of:
      1. ``_OAUTH_PROVIDER_CATALOG`` — the explicit, hand-tuned cards that carry
         bespoke flow / status_fn / cli_command (including the api-key Anthropic
         PKCE card and the synthetic claude-code subscription row, which are not
         catalog providers), and
      2. every accounts-tab provider in the unified ``provider_catalog()`` (the
         ``hermes model`` universe) — so any OAuth/external provider added as a
         plugin appears automatically, with sensible defaults, even if no
         explicit card was written for it.

    The explicit catalog wins on metadata; the unified catalog guarantees we
    never silently drop a provider the CLI picker offers. Order: explicit cards
    first (their curated order), then any catalog-only providers appended in
    ``hermes model`` order.
    """
    rows: list[Dict[str, Any]] = []
    seen: set[str] = set()

    # 1. Explicit hand-tuned cards (authoritative metadata + curated order).
    for entry in _OAUTH_PROVIDER_CATALOG:
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        rows.append(dict(entry))

    # 2. Catalog accounts-providers not already covered — keeps the Accounts tab
    #    in lockstep with the `hermes model` universe (zero-edit for new plugins).
    try:
        from hermes_cli.provider_catalog import provider_catalog
        for d in provider_catalog():
            if d.tab != "accounts" or d.slug in seen:
                continue
            seen.add(d.slug)
            rows.append({
                "id": d.slug,
                "name": d.label,
                "flow": "external",
                "cli_command": f"hermes auth add {d.slug}",
                "docs_url": d.signup_url or "",
                "status_fn": None,
            })
    except Exception:
        pass

    return rows


@app.get("/api/providers/oauth")
async def list_oauth_providers(profile: Optional[str] = None):
    """Enumerate every OAuth-capable LLM provider with current status.

    Response shape (per provider):
        id              stable identifier (used in DELETE path)
        name            human label
        flow            "pkce" | "device_code" | "external"
        cli_command     fallback CLI command for users to run manually
        disconnect_command  shell command that clears an external provider's
                            creds (run in the embedded terminal), else null
        docs_url        external docs/portal link for the "Learn more" link
        status:
          logged_in        bool — currently has usable creds
          source           short slug ("hermes_pkce", "claude_code", ...)
          source_label     human-readable origin (file path, env var name)
          token_preview    last N chars of the token, never the full token
          expires_at       ISO timestamp string or null
          has_refresh_token bool

    Membership is derived from the unified provider_catalog() so this stays in
    sync with the `hermes model` picker; _OAUTH_OVERRIDES supplies per-provider
    flow/status/cli metadata.
    """
    with _profile_scope(profile):
        providers = []
        for p in _build_oauth_catalog():
            status = _resolve_provider_status(p["id"], p.get("status_fn"))
            disconnect_hint = _oauth_provider_disconnect_hint(p, status)
            providers.append({
                "id": p["id"],
                "name": p["name"],
                "flow": p["flow"],
                "cli_command": p["cli_command"],
                "docs_url": p["docs_url"],
                "disconnect_hint": disconnect_hint,
                "disconnect_command": _oauth_provider_disconnect_command(p),
                "disconnectable": disconnect_hint is None,
                "status": status,
            })
        return {"providers": providers}


@app.delete("/api/providers/oauth/{provider_id}")
async def disconnect_oauth_provider(
    provider_id: str,
    request: Request,
    profile: Optional[str] = None,
):
    """Disconnect an OAuth provider. Token-protected (matches /env/reveal)."""

    with _profile_scope(profile):
        catalog_by_id = {p["id"]: p for p in _build_oauth_catalog()}
        provider = catalog_by_id.get(provider_id)
        if provider is None:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown provider: {provider_id}. "
                       f"Available: {', '.join(sorted(catalog_by_id))}",
            )

        disconnect_hint = _oauth_provider_disconnect_hint(provider, {})
        if disconnect_hint:
            raise HTTPException(
                status_code=400,
                detail=f"{provider['name']} cannot be disconnected automatically. {disconnect_hint}",
            )

        status = _resolve_provider_status(provider_id, provider.get("status_fn"))
        disconnect_hint = _oauth_provider_disconnect_hint(provider, status)
        if disconnect_hint:
            raise HTTPException(
                status_code=400,
                detail=f"{provider['name']} cannot be disconnected automatically. {disconnect_hint}",
            )

        # Anthropic clears only the Hermes-managed PKCE file and auth-store entry.
        # The separate claude-code catalog row is external/read-only and rejected
        # above so we never pretend to remove ~/.claude/* credentials owned by the CLI.
        if provider_id == "anthropic":
            cleared = False
            try:
                from agent.anthropic_adapter import _HERMES_OAUTH_FILE
                if _HERMES_OAUTH_FILE.exists():
                    _HERMES_OAUTH_FILE.unlink()
                    cleared = True
            except Exception:
                pass
            # Also clear the credential pool entry if present.
            try:
                from hermes_cli.auth import clear_provider_auth
                cleared = clear_provider_auth("anthropic") or cleared
            except Exception:
                pass
            _log.info("oauth/disconnect: %s", provider_id)
            return {"ok": bool(cleared), "provider": provider_id}

        try:
            from hermes_cli.auth import clear_provider_auth, invalidate_nous_auth_status_cache
            cleared = clear_provider_auth(provider_id)
            if provider_id == "nous":
                invalidate_nous_auth_status_cache()
            _log.info("oauth/disconnect: %s (cleared=%s)", provider_id, cleared)
            return {"ok": bool(cleared), "provider": provider_id}
        except Exception as e:
            _log.exception("disconnect %s failed", provider_id)
            raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# OAuth Phase 2 — in-browser PKCE & device-code flows
# ---------------------------------------------------------------------------
#
# Two flow shapes are supported:
#
#   PKCE (Anthropic):
#     1. POST /api/providers/oauth/anthropic/start
#          → server generates code_verifier + challenge, builds claude.ai
#            authorize URL, stashes verifier in _oauth_sessions[session_id]
#          → returns { session_id, flow: "pkce", auth_url }
#     2. UI opens auth_url in a new tab. User authorizes, copies code.
#     3. POST /api/providers/oauth/anthropic/submit { session_id, code }
#          → server exchanges (code + verifier) → tokens at console.anthropic.com
#          → persists to ~/.hermes/.anthropic_oauth.json AND credential pool
#          → returns { ok: true, status: "approved" }
#
#   Device code (Nous, OpenAI Codex):
#     1. POST /api/providers/oauth/{nous|openai-codex}/start
#          → server hits provider's device-auth endpoint
#          → gets { user_code, verification_url, device_code, interval, expires_in }
#          → spawns background poller thread that polls the token endpoint
#            every `interval` seconds until approved/expired
#          → stores poll status in _oauth_sessions[session_id]
#          → returns { session_id, flow: "device_code", user_code,
#                      verification_url, expires_in, poll_interval }
#     2. UI opens verification_url in a new tab and shows user_code.
#     3. UI polls GET /api/providers/oauth/{provider}/poll/{session_id}
#          every 2s until status != "pending".
#     4. On "approved" the background thread has already saved creds; UI
#        refreshes the providers list.
#
# Sessions are kept in-memory only (single-process FastAPI) and time out
# after 15 minutes. A periodic cleanup runs on each /start call to GC
# expired sessions so the dict doesn't grow without bound.

_OAUTH_SESSION_TTL_SECONDS = 15 * 60
_oauth_sessions: Dict[str, Dict[str, Any]] = {}
_oauth_sessions_lock = threading.Lock()

# Import OAuth constants from canonical source instead of duplicating.
# Guarded so hermes web still starts if anthropic_adapter is unavailable;
# Phase 2 endpoints will return 501 in that case.
try:
    from agent.anthropic_adapter import (
        _OAUTH_CLIENT_ID as _ANTHROPIC_OAUTH_CLIENT_ID,
        _OAUTH_TOKEN_URL as _ANTHROPIC_OAUTH_TOKEN_URL,
        _OAUTH_TOKEN_URLS as _ANTHROPIC_OAUTH_TOKEN_URLS,
        _OAUTH_REDIRECT_URI as _ANTHROPIC_OAUTH_REDIRECT_URI,
        _OAUTH_SCOPES as _ANTHROPIC_OAUTH_SCOPES,
        _generate_pkce as _generate_pkce_pair,
    )
    _ANTHROPIC_OAUTH_AVAILABLE = True
except ImportError:
    _ANTHROPIC_OAUTH_AVAILABLE = False
_ANTHROPIC_OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"


def _gc_oauth_sessions() -> None:
    """Drop expired sessions. Called opportunistically on /start."""
    cutoff = time.time() - _OAUTH_SESSION_TTL_SECONDS
    with _oauth_sessions_lock:
        stale = [sid for sid, sess in _oauth_sessions.items() if sess["created_at"] < cutoff]
        for sid in stale:
            _oauth_sessions.pop(sid, None)


def _oauth_profile_name(profile: Optional[str]) -> Optional[str]:
    requested = (profile or "").strip()
    if not requested or requested.lower() == "current":
        return None
    return requested


def _validate_oauth_profile(profile: Optional[str]) -> None:
    profile_name = _oauth_profile_name(profile)
    if profile_name:
        _resolve_profile_dir(profile_name)


def _new_oauth_session(
    provider_id: str,
    flow: str,
    profile: Optional[str] = None,
) -> tuple[str, Dict[str, Any]]:
    """Create + register a new OAuth session, return (session_id, session_dict)."""
    sid = secrets.token_urlsafe(16)
    profile_name = _oauth_profile_name(profile)
    sess = {
        "session_id": sid,
        "provider": provider_id,
        "flow": flow,
        "profile": profile_name,
        "created_at": time.time(),
        "status": "pending",  # pending | approved | denied | expired | error
        "error_message": None,
    }
    with _oauth_sessions_lock:
        _oauth_sessions[sid] = sess
    return sid, sess


def _oauth_session_profile(
    session_id: str,
    fallback: Optional[str] = None,
) -> Optional[str]:
    """Return the profile that owns an OAuth session, if one was provided."""
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
        profile = sess.get("profile") if sess else None
    return profile or _oauth_profile_name(fallback)


def _save_anthropic_oauth_creds(access_token: str, refresh_token: str, expires_at_ms: int) -> None:
    """Persist Anthropic PKCE creds to both Hermes file AND credential pool.

    Mirrors what auth_commands.add_command does so the dashboard flow leaves
    the system in the same state as ``hermes auth add anthropic``.
    """
    from agent.anthropic_adapter import _HERMES_OAUTH_FILE
    payload = {
        "accessToken": access_token,
        "refreshToken": refresh_token,
        "expiresAt": expires_at_ms,
    }
    _HERMES_OAUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _HERMES_OAUTH_FILE.with_name(
        f"{_HERMES_OAUTH_FILE.name}.tmp.{os.getpid()}.{secrets.token_hex(8)}"
    )
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, _HERMES_OAUTH_FILE)
        try:
            _HERMES_OAUTH_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
    # Best-effort credential-pool insert. Failure here doesn't invalidate
    # the file write — pool registration only matters for the rotation
    # strategy, not for runtime credential resolution.
    try:
        from agent.credential_pool import (
            PooledCredential,
            load_pool,
            AUTH_TYPE_OAUTH,
            SOURCE_MANUAL,
        )
        import uuid
        pool = load_pool("anthropic")
        # Avoid duplicate entries: delete any prior dashboard-issued OAuth entry
        existing = [e for e in pool.entries() if getattr(e, "source", "").startswith(f"{SOURCE_MANUAL}:dashboard_pkce")]
        for e in existing:
            try:
                pool.remove_entry(getattr(e, "id", ""))
            except Exception:
                pass
        entry = PooledCredential(
            provider="anthropic",
            id=uuid.uuid4().hex[:6],
            label="dashboard PKCE",
            auth_type=AUTH_TYPE_OAUTH,
            priority=0,
            source=f"{SOURCE_MANUAL}:dashboard_pkce",
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at_ms=expires_at_ms,
        )
        pool.add_entry(entry)
    except Exception as e:
        _log.warning("anthropic pool add (dashboard) failed: %s", e)


def _start_anthropic_pkce(profile: Optional[str] = None) -> Dict[str, Any]:
    """Begin PKCE flow. Returns the auth URL the UI should open."""
    if not _ANTHROPIC_OAUTH_AVAILABLE:
        raise HTTPException(status_code=501, detail="Anthropic OAuth not available (missing adapter)")
    verifier, challenge = _generate_pkce_pair()
    sid, sess = _new_oauth_session("anthropic", "pkce", profile=profile)
    sess["verifier"] = verifier
    sess["state"] = verifier  # Anthropic round-trips verifier as state
    params = {
        "code": "true",
        "client_id": _ANTHROPIC_OAUTH_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": _ANTHROPIC_OAUTH_REDIRECT_URI,
        "scope": _ANTHROPIC_OAUTH_SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": verifier,
    }
    auth_url = f"{_ANTHROPIC_OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"
    return {
        "session_id": sid,
        "flow": "pkce",
        "auth_url": auth_url,
        "expires_in": _OAUTH_SESSION_TTL_SECONDS,
    }


def _submit_anthropic_pkce(
    session_id: str,
    code_input: str,
    profile: Optional[str] = None,
) -> Dict[str, Any]:
    """Exchange authorization code for tokens. Persists on success."""
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess or sess["provider"] != "anthropic" or sess["flow"] != "pkce":
        raise HTTPException(status_code=404, detail="Unknown or expired session")
    if sess["status"] != "pending":
        return {"ok": False, "status": sess["status"], "message": sess.get("error_message")}

    # Anthropic's redirect callback page formats the code as `<code>#<state>`.
    # Strip the state suffix if present (we already have the verifier server-side).
    parts = code_input.strip().split("#", 1)
    code = parts[0].strip()
    if not code:
        return {"ok": False, "status": "error", "message": "No code provided"}
    state_from_callback = parts[1] if len(parts) > 1 else ""

    exchange_data = json.dumps({
        "grant_type": "authorization_code",
        "client_id": _ANTHROPIC_OAUTH_CLIENT_ID,
        "code": code,
        "state": state_from_callback or sess["state"],
        "redirect_uri": _ANTHROPIC_OAUTH_REDIRECT_URI,
        "code_verifier": sess["verifier"],
    }).encode()
    # Anthropic migrated the OAuth token endpoint to platform.claude.com;
    # console.anthropic.com now 404s. Try the new host first, then fall back.
    result = None
    last_exc = None
    for _endpoint in _ANTHROPIC_OAUTH_TOKEN_URLS:
        req = urllib.request.Request(
            _endpoint,
            data=exchange_data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "hermes-dashboard/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read().decode())
            break
        except Exception as e:
            last_exc = e
            continue
    if result is None:
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = f"Token exchange failed: {last_exc}"
        return {"ok": False, "status": "error", "message": sess["error_message"]}

    access_token = result.get("access_token", "")
    refresh_token = result.get("refresh_token", "")
    expires_in = int(result.get("expires_in") or 3600)
    if not access_token:
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = "No access token returned"
        return {"ok": False, "status": "error", "message": sess["error_message"]}

    expires_at_ms = int(time.time() * 1000) + (expires_in * 1000)
    try:
        with _profile_scope(_oauth_session_profile(session_id, profile)):
            _save_anthropic_oauth_creds(access_token, refresh_token, expires_at_ms)
    except Exception as e:
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = f"Save failed: {e}"
        return {"ok": False, "status": "error", "message": sess["error_message"]}
    with _oauth_sessions_lock:
        sess["status"] = "approved"
    _log.info("oauth/pkce: anthropic login completed (session=%s)", session_id)
    return {"ok": True, "status": "approved"}


async def _start_device_code_flow(
    provider_id: str,
    profile: Optional[str] = None,
) -> Dict[str, Any]:
    """Initiate a device-code flow (Nous, OpenAI Codex, MiniMax, or xAI).

    Calls the provider's device-auth endpoint via the existing CLI helpers,
    then spawns a background poller. Returns the user-facing display fields
    so the UI can render the verification page link + user code.
    """
    if provider_id == "nous":
        from hermes_cli.auth import (
            _request_device_code,
            PROVIDER_REGISTRY,
        )
        import httpx
        pconfig = PROVIDER_REGISTRY["nous"]
        portal_base_url = (
            os.getenv("HERMES_PORTAL_BASE_URL")
            or os.getenv("NOUS_PORTAL_BASE_URL")
            or pconfig.portal_base_url
        ).rstrip("/")
        client_id = pconfig.client_id
        scope = pconfig.scope

        def _do_nous_device_request():
            with httpx.Client(
                timeout=httpx.Timeout(15.0),
                headers={"Accept": "application/json"},
            ) as client:
                return (
                    _request_device_code(
                        client=client,
                        portal_base_url=portal_base_url,
                        client_id=client_id,
                        scope=scope,
                    ),
                    scope,
                )

        device_data, effective_scope = await asyncio.get_running_loop().run_in_executor(
            None, _do_nous_device_request
        )
        sid, sess = _new_oauth_session("nous", "device_code", profile=profile)
        sess["device_code"] = str(device_data["device_code"])
        sess["interval"] = int(device_data["interval"])
        sess["expires_at"] = time.time() + int(device_data["expires_in"])
        sess["portal_base_url"] = portal_base_url
        sess["client_id"] = client_id
        sess["scope"] = effective_scope
        threading.Thread(
            target=_nous_poller, args=(sid,), daemon=True, name=f"oauth-poll-{sid[:6]}"
        ).start()
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": str(device_data["user_code"]),
            "verification_url": str(device_data["verification_uri_complete"]),
            "expires_in": int(device_data["expires_in"]),
            "poll_interval": int(device_data["interval"]),
        }

    if provider_id == "openai-codex":
        # Codex uses fixed OpenAI device-auth endpoints; reuse the helper.
        sid, _ = _new_oauth_session("openai-codex", "device_code", profile=profile)
        # Use the helper but in a thread because it polls inline.
        # We can't extract just the start step without refactoring auth.py,
        # so we run the full helper in a worker and proxy the user_code +
        # verification_url back via the session dict. The helper prints
        # to stdout — we capture nothing here, just status.
        threading.Thread(
            target=_codex_full_login_worker, args=(sid,), daemon=True,
            name=f"oauth-codex-{sid[:6]}",
        ).start()
        # Block briefly until the worker has populated the user_code, OR error.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with _oauth_sessions_lock:
                s = _oauth_sessions.get(sid)
            if s and (s.get("user_code") or s["status"] != "pending"):
                break
            await asyncio.sleep(0.1)
        with _oauth_sessions_lock:
            s = _oauth_sessions.get(sid, {})
        if s.get("status") == "error":
            raise HTTPException(status_code=500, detail=s.get("error_message") or "device-auth failed")
        if not s.get("user_code"):
            raise HTTPException(status_code=504, detail="device-auth timed out before returning a user code")
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": s["user_code"],
            "verification_url": s["verification_url"],
            "expires_in": int(s.get("expires_in") or 900),
            "poll_interval": int(s.get("interval") or 5),
        }

    if provider_id == "minimax-oauth":
        # MiniMax uses a device-code-style flow (verification URI + user
        # code + background poll) with a PKCE extension on top. From the
        # operator's perspective it's identical to Nous's device-code
        # flow; the PKCE bit (verifier + challenge from
        # _minimax_pkce_pair) is a security extension that binds the
        # token exchange to the original session.
        from hermes_cli.auth import (
            _minimax_pkce_pair,
            _minimax_request_user_code,
            MINIMAX_OAUTH_CLIENT_ID,
            MINIMAX_OAUTH_GLOBAL_BASE,
        )
        import httpx
        verifier, challenge, state = _minimax_pkce_pair()
        portal_base_url = (
            os.getenv("MINIMAX_PORTAL_BASE_URL") or MINIMAX_OAUTH_GLOBAL_BASE
        ).rstrip("/")
        def _do_minimax_request():
            with httpx.Client(
                timeout=httpx.Timeout(15.0),
                headers={"Accept": "application/json"},
                follow_redirects=True,
            ) as client:
                return _minimax_request_user_code(
                    client=client,
                    portal_base_url=portal_base_url,
                    client_id=MINIMAX_OAUTH_CLIENT_ID,
                    code_challenge=challenge,
                    state=state,
                )
        device_data = await asyncio.get_event_loop().run_in_executor(
            None, _do_minimax_request
        )
        sid, sess = _new_oauth_session("minimax-oauth", "device_code", profile=profile)
        # The CLI flow names this `interval_ms` because MiniMax's
        # `interval` field is in milliseconds (defensive default 2000ms
        # in _minimax_poll_token).
        interval_raw = device_data.get("interval")
        sess["interval_ms"] = (
            int(interval_raw) if interval_raw is not None else None
        )
        sess["user_code"] = str(device_data["user_code"])
        sess["code_verifier"] = verifier
        sess["state"] = state
        sess["portal_base_url"] = portal_base_url
        sess["client_id"] = MINIMAX_OAUTH_CLIENT_ID
        sess["region"] = "global"
        # `expired_in` from MiniMax is overloaded — could be a unix-ms
        # timestamp OR a seconds-from-now duration. Mirror the heuristic
        # in _minimax_poll_token. Stash the raw value for the poller;
        # compute a derived expires_at + UI-friendly expires_in seconds.
        expired_in_raw = int(device_data["expired_in"])
        sess["expired_in_raw"] = expired_in_raw
        if expired_in_raw > 1_000_000_000_000:  # likely unix-ms
            expires_at_ts = expired_in_raw / 1000.0
            expires_in_seconds = max(0, int(expires_at_ts - time.time()))
        else:
            expires_at_ts = time.time() + expired_in_raw
            expires_in_seconds = expired_in_raw
        sess["expires_at"] = expires_at_ts
        threading.Thread(
            target=_minimax_poller,
            args=(sid,),
            daemon=True,
            name=f"oauth-poll-{sid[:6]}",
        ).start()
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": str(device_data["user_code"]),
            "verification_url": str(device_data["verification_uri"]),
            "expires_in": expires_in_seconds,
            "poll_interval": max(2, (sess["interval_ms"] or 2000) // 1000),
        }

    if provider_id == "xai-oauth":
        from hermes_cli.auth import _xai_oauth_request_device_code
        import httpx

        def _do_xai_device_request():
            with httpx.Client(
                timeout=httpx.Timeout(20.0),
                headers={"Accept": "application/json"},
            ) as client:
                return _xai_oauth_request_device_code(client)

        device_data = await asyncio.get_running_loop().run_in_executor(
            None, _do_xai_device_request
        )
        sid, sess = _new_oauth_session("xai-oauth", "device_code", profile=profile)
        sess["device_code"] = str(device_data["device_code"])
        sess["interval"] = int(device_data["interval"])
        sess["expires_at"] = time.time() + int(device_data["expires_in"])
        threading.Thread(
            target=_xai_device_poller,
            args=(sid,),
            daemon=True,
            name=f"oauth-poll-{sid[:6]}",
        ).start()
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": str(device_data["user_code"]),
            "verification_url": str(
                device_data.get("verification_uri_complete")
                or device_data["verification_uri"]
            ),
            "expires_in": int(device_data["expires_in"]),
            "poll_interval": int(device_data["interval"]),
        }

    raise HTTPException(status_code=400, detail=f"Provider {provider_id} does not support device-code flow")


def _nous_poller(session_id: str) -> None:
    """Background poller that drives a Nous device-code flow to completion."""
    from hermes_cli.auth import (
        _poll_for_token,
        refresh_nous_oauth_from_state,
    )
    from datetime import datetime, timezone
    import httpx
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        return
    portal_base_url = sess["portal_base_url"]
    client_id = sess["client_id"]
    device_code = sess["device_code"]
    interval = sess["interval"]
    scope = sess.get("scope")
    expires_in = max(60, int(sess["expires_at"] - time.time()))
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0), headers={"Accept": "application/json"}) as client:
            token_data = _poll_for_token(
                client=client,
                portal_base_url=portal_base_url,
                client_id=client_id,
                device_code=device_code,
                expires_in=expires_in,
                poll_interval=interval,
            )
        # Same post-processing as _nous_device_code_login (validate/refresh JWT)
        now = datetime.now(timezone.utc)
        token_ttl = int(token_data.get("expires_in") or 0)
        auth_state = {
            "portal_base_url": portal_base_url,
            "inference_base_url": token_data.get("inference_base_url"),
            "client_id": client_id,
            "scope": token_data.get("scope") or scope,
            "token_type": token_data.get("token_type", "Bearer"),
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token"),
            "obtained_at": now.isoformat(),
            "expires_at": (
                datetime.fromtimestamp(now.timestamp() + token_ttl, tz=timezone.utc).isoformat()
                if token_ttl else None
            ),
            "expires_in": token_ttl,
        }
        with _profile_scope(_oauth_session_profile(session_id)):
            full_state = refresh_nous_oauth_from_state(
                auth_state,
                timeout_seconds=15.0,
                force_refresh=False,
            )
            from hermes_cli.auth import persist_nous_credentials
            persist_nous_credentials(full_state)
        with _oauth_sessions_lock:
            sess["status"] = "approved"
        _log.info("oauth/device: nous login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("nous device-code poll failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = str(e)


def _minimax_poller(session_id: str) -> None:
    """Background poller that drives a MiniMax OAuth flow to completion.

    Mirrors `_nous_poller` but calls the MiniMax-specific token endpoint,
    which uses a PKCE-style ``code_verifier`` + ``user_code`` rather than
    the ``device_code`` field used by Nous. On success, builds the same
    auth_state dict that ``_minimax_oauth_login`` (the CLI flow) builds
    and persists via ``_minimax_save_auth_state`` — so the dashboard
    path leaves the system in the same state as
    ``hermes auth add minimax-oauth``.
    """
    from hermes_cli.auth import (
        _minimax_poll_token,
        _minimax_resolve_token_expiry_unix,
        _minimax_save_auth_state,
        MINIMAX_OAUTH_GLOBAL_INFERENCE,
        MINIMAX_OAUTH_SCOPE,
    )
    from datetime import datetime, timezone
    import httpx
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        return
    portal_base_url = sess["portal_base_url"]
    client_id = sess["client_id"]
    user_code = sess["user_code"]
    code_verifier = sess["code_verifier"]
    interval_ms = sess.get("interval_ms")
    expired_in_raw = sess["expired_in_raw"]
    try:
        with httpx.Client(
            timeout=httpx.Timeout(15.0),
            headers={"Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            token_data = _minimax_poll_token(
                client=client,
                portal_base_url=portal_base_url,
                client_id=client_id,
                user_code=user_code,
                code_verifier=code_verifier,
                expired_in=expired_in_raw,
                interval_ms=interval_ms,
            )
        # Build the auth_state dict in the same shape as the CLI flow's
        # `_minimax_oauth_login` so `_minimax_save_auth_state` writes
        # the canonical record. Region is fixed to "global" for the
        # dashboard path; cn-region operators can still use the CLI
        # flow which supports `--region cn`.
        now = datetime.now(timezone.utc)
        expires_at_ts = _minimax_resolve_token_expiry_unix(
            int(token_data["expired_in"]), now=now,
        )
        expires_in_s = max(0, int(expires_at_ts - now.timestamp()))
        auth_state = {
            "provider": "minimax-oauth",
            "region": sess.get("region", "global"),
            "portal_base_url": portal_base_url,
            "inference_base_url": MINIMAX_OAUTH_GLOBAL_INFERENCE,
            "client_id": client_id,
            "scope": MINIMAX_OAUTH_SCOPE,
            "token_type": token_data.get("token_type", "Bearer"),
            "access_token": token_data["access_token"],
            "refresh_token": token_data["refresh_token"],
            "resource_url": token_data.get("resource_url"),
            "obtained_at": now.isoformat(),
            "expires_at": datetime.fromtimestamp(
                expires_at_ts, tz=timezone.utc
            ).isoformat(),
            "expires_in": expires_in_s,
        }
        with _profile_scope(_oauth_session_profile(session_id)):
            _minimax_save_auth_state(auth_state)
        with _oauth_sessions_lock:
            sess["status"] = "approved"
        _log.info("oauth/device: minimax login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("minimax device-code poll failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = str(e)


def _xai_device_poller(session_id: str) -> None:
    """Background poller for xAI's OAuth device-code flow."""
    import httpx
    from hermes_cli.auth import (
        _save_xai_oauth_tokens,
        _xai_oauth_discovery,
        _xai_oauth_poll_device_token,
        unsuppress_credential_source,
    )

    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        return
    device_code = sess["device_code"]
    interval = int(sess["interval"])
    expires_in = max(60, int(sess["expires_at"] - time.time()))
    try:
        discovery = _xai_oauth_discovery(20.0)
        with httpx.Client(
            timeout=httpx.Timeout(20.0),
            headers={"Accept": "application/json"},
        ) as client:
            token_data = _xai_oauth_poll_device_token(
                client,
                token_endpoint=discovery["token_endpoint"],
                device_code=device_code,
                expires_in=expires_in,
                poll_interval=interval,
            )
        tokens = {
            "access_token": str(token_data.get("access_token", "") or "").strip(),
            "refresh_token": str(token_data.get("refresh_token", "") or "").strip(),
            "id_token": str(token_data.get("id_token", "") or "").strip(),
            "expires_in": token_data.get("expires_in"),
            "token_type": str(token_data.get("token_type") or "Bearer").strip() or "Bearer",
        }
        with _profile_scope(_oauth_session_profile(session_id)):
            _save_xai_oauth_tokens(
                tokens,
                discovery=discovery,
                last_refresh=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                auth_mode="oauth_device_code",
            )
            # The singleton write above is the single source of truth: the
            # credential-pool load seeds it as the canonical ``device_code``
            # entry. Do NOT also insert a parallel ``manual:dashboard_*`` pool
            # entry — that duplicates the single-use refresh token across two
            # entries and triggers rotation churn / ``refresh_token_reused``.
            # An interactive dashboard login is also an explicit re-enable
            # signal, so clear any ``device_code`` suppression left by a
            # prior ``hermes auth remove xai-oauth`` (mirrors auth_add_command
            # and the ``hermes model`` re-login path in _login_xai_oauth).
            unsuppress_credential_source("xai-oauth", "device_code")
        with _oauth_sessions_lock:
            sess["status"] = "approved"
        _log.info("oauth/device: xai login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("xai device-code poll failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            sess["status"] = "error"
            sess["error_message"] = str(e)


def _codex_full_login_worker(session_id: str) -> None:
    """Run the complete OpenAI Codex device-code flow.

    Codex doesn't use the standard OAuth device-code endpoints; it has its
    own ``/api/accounts/deviceauth/usercode`` (JSON body, returns
    ``device_auth_id``) and ``/api/accounts/deviceauth/token`` (JSON body
    polled until 200). On success the response carries an
    ``authorization_code`` + ``code_verifier`` that get exchanged at
    CODEX_OAUTH_TOKEN_URL with grant_type=authorization_code.

    The flow is replicated inline (rather than calling
    _codex_device_code_login) because that helper prints/blocks/polls in a
    single function — we need to surface the user_code to the dashboard the
    moment we receive it, well before polling completes.
    """
    try:
        import httpx
        from hermes_cli.auth import (
            CODEX_OAUTH_CLIENT_ID,
            CODEX_OAUTH_TOKEN_URL,
            DEFAULT_CODEX_BASE_URL,
        )
        issuer = "https://auth.openai.com"

        # Step 1: request device code
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            resp = client.post(
                f"{issuer}/api/accounts/deviceauth/usercode",
                json={"client_id": CODEX_OAUTH_CLIENT_ID},
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code != 200:
            raise RuntimeError(f"deviceauth/usercode returned {resp.status_code}")
        device_data = resp.json()
        user_code = device_data.get("user_code", "")
        device_auth_id = device_data.get("device_auth_id", "")
        poll_interval = max(3, int(device_data.get("interval", "5")))
        if not user_code or not device_auth_id:
            raise RuntimeError("device-code response missing user_code or device_auth_id")
        verification_url = f"{issuer}/codex/device"
        with _oauth_sessions_lock:
            sess = _oauth_sessions.get(session_id)
            if not sess:
                return
            sess["user_code"] = user_code
            sess["verification_url"] = verification_url
            sess["device_auth_id"] = device_auth_id
            sess["interval"] = poll_interval
            sess["expires_in"] = 15 * 60  # OpenAI's effective limit
            sess["expires_at"] = time.time() + sess["expires_in"]

        # Step 2: poll until authorized
        deadline = time.monotonic() + sess["expires_in"]
        code_resp = None
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            while time.monotonic() < deadline:
                time.sleep(poll_interval)
                poll = client.post(
                    f"{issuer}/api/accounts/deviceauth/token",
                    json={"device_auth_id": device_auth_id, "user_code": user_code},
                    headers={"Content-Type": "application/json"},
                )
                if poll.status_code == 200:
                    code_resp = poll.json()
                    break
                if poll.status_code in {403, 404}:
                    continue  # user hasn't authorized yet
                raise RuntimeError(f"deviceauth/token poll returned {poll.status_code}")

        if code_resp is None:
            with _oauth_sessions_lock:
                sess["status"] = "expired"
                sess["error_message"] = "Device code expired before approval"
            return

        # Step 3: exchange authorization_code for tokens
        authorization_code = code_resp.get("authorization_code", "")
        code_verifier = code_resp.get("code_verifier", "")
        if not authorization_code or not code_verifier:
            raise RuntimeError("device-auth response missing authorization_code/code_verifier")
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            token_resp = client.post(
                CODEX_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "redirect_uri": f"{issuer}/deviceauth/callback",
                    "client_id": CODEX_OAUTH_CLIENT_ID,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if token_resp.status_code != 200:
            raise RuntimeError(f"token exchange returned {token_resp.status_code}")
        tokens = token_resp.json()
        access_token = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token", "")
        if not access_token:
            raise RuntimeError("token exchange did not return access_token")

        from hermes_cli.auth import _save_codex_tokens

        with _profile_scope(_oauth_session_profile(session_id)):
            _save_codex_tokens({
                "access_token": access_token,
                "refresh_token": refresh_token,
            })
        with _oauth_sessions_lock:
            sess["status"] = "approved"
        _log.info("oauth/device: openai-codex login completed (session=%s)", session_id)
    except Exception as e:
        _log.warning("codex device-code worker failed (session=%s): %s", session_id, e)
        with _oauth_sessions_lock:
            s = _oauth_sessions.get(session_id)
            if s:
                s["status"] = "error"
                s["error_message"] = str(e)


@app.post("/api/providers/oauth/{provider_id}/start")
async def start_oauth_login(
    provider_id: str,
    request: Request,
    profile: Optional[str] = None,
):
    """Initiate an OAuth login flow. Token-protected."""
    _gc_oauth_sessions()
    _validate_oauth_profile(profile)
    valid = {p["id"] for p in _OAUTH_PROVIDER_CATALOG}
    if provider_id not in valid:
        raise HTTPException(status_code=400, detail=f"Unknown provider {provider_id}")
    catalog_entry = next(p for p in _OAUTH_PROVIDER_CATALOG if p["id"] == provider_id)
    if catalog_entry["flow"] == "external":
        raise HTTPException(
            status_code=400,
            detail=f"{provider_id} uses an external CLI; run `{catalog_entry['cli_command']}` manually",
        )
    try:
        # The pkce branch is gated on provider_id == "anthropic" because
        # `_start_anthropic_pkce()` is hardcoded to the Anthropic flow.
        # Routing any other future pkce-flagged provider through it would
        # silently launch the Anthropic OAuth flow (the bug fixed in this
        # change for MiniMax). New PKCE providers must add their own
        # start function and an explicit branch here.
        if catalog_entry["flow"] == "pkce" and provider_id == "anthropic":
            return _start_anthropic_pkce(profile=profile)
        if catalog_entry["flow"] == "device_code":
            return await _start_device_code_flow(provider_id, profile=profile)
    except HTTPException:
        raise
    except Exception as e:
        _log.exception("oauth/start %s failed", provider_id)
        raise HTTPException(status_code=500, detail=str(e))
    raise HTTPException(status_code=400, detail="Unsupported flow")


class OAuthSubmitBody(BaseModel):
    session_id: str
    code: str


@app.post("/api/providers/oauth/{provider_id}/submit")
async def submit_oauth_code(
    provider_id: str,
    body: OAuthSubmitBody,
    request: Request,
    profile: Optional[str] = None,
):
    """Submit the auth code for PKCE flows. Token-protected."""
    if provider_id == "anthropic":
        return await asyncio.get_running_loop().run_in_executor(
            None, _submit_anthropic_pkce, body.session_id, body.code, profile,
        )
    raise HTTPException(status_code=400, detail=f"submit not supported for {provider_id}")


@app.get("/api/providers/oauth/{provider_id}/poll/{session_id}")
async def poll_oauth_session(
    provider_id: str,
    session_id: str,
    profile: Optional[str] = None,
):
    """Poll a session's status (no auth — read-only state).

    Shared by the device-code flows (Nous, OpenAI Codex, MiniMax, xAI).
    Each surfaces progress through the same background-worker-updated
    ``status`` field, so a single poll endpoint serves them all.
    """
    with _oauth_sessions_lock:
        sess = _oauth_sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if sess["provider"] != provider_id:
        raise HTTPException(status_code=400, detail="Provider mismatch for session")
    return {
        "session_id": session_id,
        "status": sess["status"],
        "error_message": sess.get("error_message"),
        "expires_at": sess.get("expires_at"),
    }


@app.delete("/api/providers/oauth/sessions/{session_id}")
async def cancel_oauth_session(
    session_id: str,
    request: Request,
    profile: Optional[str] = None,
):
    """Cancel a pending OAuth session. Token-protected."""
    with _oauth_sessions_lock:
        sess = _oauth_sessions.pop(session_id, None)
    if sess is None:
        return {"ok": False, "message": "session not found"}
    return {"ok": True, "session_id": session_id}


# ---------------------------------------------------------------------------
# Session detail endpoints
# ---------------------------------------------------------------------------


def _session_latest_descendant(session_id: str):
    """Resolve a session id to the newest child leaf session."""
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        return session_api.session_latest_descendant(db, session_id)
    finally:
        db.close()


# CRITICAL — every literal-path route below MUST be declared BEFORE the
# templated ``/api/sessions/{session_id}`` family that follows. FastAPI/
# Starlette match routes in registration order, and the ``{session_id}``
# pattern is unconstrained — it would otherwise swallow e.g.
# ``DELETE /api/sessions/empty``, ``POST /api/sessions/bulk-delete``, or
# ``GET /api/sessions/stats`` as "operate on the session with id
# 'empty'" / "'bulk-delete'" / "'stats'", which would 404 (or worse,
# succeed and delete the wrong row). Same story as the older
# ``/api/sessions/search`` endpoint up at line ~1191. If you split or
# reorder this block, move every route in it together.
class BulkDeleteSessions(BaseModel):
    ids: List[str]
    profile: Optional[str] = None


@app.post("/api/sessions/bulk-delete")
async def bulk_delete_sessions_endpoint(request: Request, body: BulkDeleteSessions):
    """Delete every session in ``body.ids`` in a single DB transaction.

    Backs the dashboard's bulk-select-and-delete flow on the sessions
    page. POST (not DELETE) because most HTTP clients refuse to send a
    request body on DELETE and a body is the natural shape for a list
    of IDs — Starlette accepts both, but POSTing a list keeps proxies,
    curl, and the browser ``fetch`` API consistent.

    Per-row contract matches :meth:`SessionDB.delete_sessions`:

    * Unknown IDs are silently skipped (the response ``deleted`` count
      reflects what really happened, not the input length). This is
      deliberate — UI selection state can race against another tab's
      delete, and we'd rather succeed-on-the-rest than fail-the-whole-
      batch.
    * Children of every deleted parent are orphaned, not cascade-
      deleted.
    * Active and archived sessions ARE deleted when explicitly
      selected — unlike ``DELETE /api/sessions/empty``, the user
      hand-picked the rows so we trust the selection.
    * Like the other session-delete endpoints, this does NOT pass a
      ``sessions_dir`` through; on-disk transcript / request-dump
      cleanup runs at the CLI/agent layer on the next prune pass.

    The response carries the actual deleted count, so the dashboard
    can surface it in a toast. The IDs that were removed are not
    echoed back because the client already knows what it asked to
    delete (unknown IDs are silently skipped — see contract above)
    and can prune its in-memory list directly from the request.
    """
    # Enforce a hard cap so a runaway/typo'd selection can't lock the
    # DB writer for an extended window. The dashboard pages 20 rows
    # at a time; 500 covers a "select all on every page in a
    # reasonable scrollback" worst case without opening the door to
    # multi-thousand-row transactions.
    if len(body.ids) > 500:
        raise HTTPException(
            status_code=400,
            detail="ids must contain at most 500 entries",
        )
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(body.profile)
        return await _proxy_authenticated_owner_http(request)
    db = _open_session_db_for_profile(body.profile)
    try:
        deleted = db.delete_sessions(body.ids)
        return {"ok": True, "deleted": deleted}
    finally:
        db.close()


@app.get("/api/sessions/empty/count")
async def count_empty_sessions_endpoint(request: Request, profile: Optional[str] = None):
    """Return the number of empty, ended, non-archived sessions.

    Drives the dashboard's "Delete empty (N)" button — when N is 0 the
    UI hides the affordance so users aren't presented with a button
    that does nothing. Cheap, single-COUNT query.
    """
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_session_reader_http(request)
    db = _open_session_db_for_profile(profile)
    try:
        return {"count": db.count_empty_sessions()}
    finally:
        db.close()


@app.delete("/api/sessions/empty")
async def delete_empty_sessions_endpoint(request: Request, profile: Optional[str] = None):
    """Delete every empty (``message_count == 0``), ended,
    non-archived session in a single transaction.

    Safety contract mirrors :meth:`SessionDB.delete_empty_sessions`:

    * Active sessions are skipped (``ended_at IS NULL``) so a live
      agent isn't yanked mid-handshake.
    * Archived sessions are skipped — the user explicitly chose to
      keep those rows.
    * Children of deleted parents are orphaned, not cascade-deleted.

    Like the single-session ``DELETE /api/sessions/{id}`` endpoint
    below, this doesn't pass a ``sessions_dir`` through — the on-disk
    transcript / request-dump cleanup is wired at the CLI/agent layer
    but the web server historically leaves file cleanup to the next
    prune-on-startup pass. Matching that pre-existing trade-off keeps
    the two delete endpoints' DB-vs-disk behaviour consistent.
    """
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_owner_http(request)
    db = _open_session_db_for_profile(profile)
    try:
        deleted = db.delete_empty_sessions()
        return {"ok": True, "deleted": deleted}
    finally:
        db.close()


@app.get("/api/sessions/stats")
async def get_session_stats(request: Request, profile: Optional[str] = None):
    """Session-store statistics for the Sessions page (mirrors `hermes sessions stats`).

    Registered before ``/api/sessions/{session_id}`` so the literal ``stats``
    path isn't captured as a session id by the parameterized route.
    """
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_session_reader_http(request)
    db = _open_session_db_for_profile(profile)
    try:
        total = db.session_count(include_archived=True)
        active_store = db.session_count(include_archived=False)
        archived = db.session_count(archived_only=True)
        messages = db.message_count()
        by_source: Dict[str, int] = {}
        try:
            for s in db.list_sessions_rich(limit=10000, include_archived=True):
                src = str(s.get("source") or "cli")
                by_source[src] = by_source.get(src, 0) + 1
        except Exception:
            pass
        return {
            "total": total,
            "active_store": active_store,
            "archived": archived,
            "messages": messages,
            "by_source": by_source,
        }
    finally:
        db.close()


def _open_session_db_for_profile(profile: Optional[str], *, request: Request | None = None):
    """Open a legacy-local SessionDB, never an authenticated owner's store.

    Every authenticated session route must proxy before reaching this helper.
    Passing its request makes a future route regression fail closed instead of
    silently selecting the Control Plane/global default database.

    ``profile`` None/empty → this process's own ``state.db`` (the common,
    single-profile case). A named profile opens that profile's on-disk
    ``state.db`` directly so the primary backend can serve cross-profile reads
    (transcripts, detail) without spawning that profile's backend.
    """
    if getattr(app.state, "auth_required", False):
        _log.error("authenticated Control Plane attempted to open SessionDB directly")
        raise HTTPException(
            status_code=500,
            detail="Owner-scoped sessions must be served by an owner-scoped session service",
        )
    from hermes_state import SessionDB
    if not profile:
        return SessionDB()
    _name, home = _cron_profile_home(profile)
    return SessionDB(db_path=Path(home) / "state.db")


@app.get("/api/sessions/{session_id}")
async def get_session_detail(request: Request, session_id: str, profile: Optional[str] = None):
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_session_reader_http(request)
    db = _open_session_db_for_profile(profile)
    try:
        sid = db.resolve_session_id(session_id)
        session = db.get_session(sid) if sid else None
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        if profile:
            session["profile"] = _cron_profile_home(profile)[0]
        return session
    finally:
        db.close()


@app.get("/api/sessions/{session_id}/latest-descendant")
async def get_session_latest_descendant(request: Request, session_id: str):
    if _authenticated_owner_request(request):
        return await _proxy_authenticated_session_reader_http(request)
    latency_started_at = time.monotonic()
    latency_trace_id = request.headers.get("x-request-id", "")
    log_latency_stage(
        _log,
        trace_id=latency_trace_id,
        surface="latest-descendant",
        stage="request.received",
    )
    latest, path = _session_latest_descendant(session_id)
    if not latest:
        raise HTTPException(status_code=404, detail="Session not found")
    payload = {
        "requested_session_id": path[0] if path else session_id,
        "session_id": latest,
        "path": path,
        "changed": bool(path and latest != path[0]),
    }
    log_latency_stage(
        _log,
        trace_id=latency_trace_id,
        surface="latest-descendant",
        stage="response.ready",
        started_at=latency_started_at,
    )
    return payload

@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(
    request: Request,
    session_id: str,
    profile: Optional[str] = None,
    limit: Optional[int] = None,
    before: Optional[str] = None,
):
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_session_reader_http(request)
    db = _open_session_db_for_profile(profile)
    try:
        return session_api.session_messages_payload(
            db,
            session_id,
            limit=limit,
            before=before,
        )
    finally:
        db.close()


@app.delete("/api/sessions/{session_id}")
async def delete_session_endpoint(request: Request, session_id: str, profile: Optional[str] = None):
    # ``profile`` deletes a session belonging to another (local) profile by
    # opening its state.db directly. Remote profiles never reach here — the
    # desktop routes their DELETE to the remote backend. Omit for current/default.
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_owner_http(request)
    db = _open_session_db_for_profile(profile)
    try:
        # Resolve exact ids / unique prefixes like every other session endpoint
        # (detail, messages, rename, export all do). A session that no longer
        # exists is an idempotent success: DELETE's contract is "ensure it's
        # gone", and the desktop optimistically removes the row then RESTORES it
        # on any error — so a 404 on an already-absent row resurrected a ghost
        # row and surfaced "session not found". /goal + auto-compression churn
        # leaves transient empty rows (reaped by empty-session hygiene) that
        # race the sidebar snapshot, which is exactly when this fired. Mirrors
        # the bulk-delete endpoint, which already treats ghost ids as success.
        sid = db.resolve_session_id(session_id)
        if not sid:
            return {"ok": True, "already_absent": True}
        db.delete_session(sid)
        return {"ok": True}
    finally:
        db.close()


class SessionRename(BaseModel):
    title: Optional[str] = None
    archived: Optional[bool] = None
    # Mutate a session belonging to another profile (opens its state.db). Omit
    # for the current/default profile.
    profile: Optional[str] = None


@app.patch("/api/sessions/{session_id}")
async def rename_session_endpoint(request: Request, session_id: str, body: SessionRename):
    """Update a session: rename (or clear its title) and/or archive it.

    ``title`` renames (empty/null clears the title); ``archived`` soft-hides or
    restores the session. Either field may be omitted. ``profile`` targets
    another profile's session.
    """
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(body.profile)
        return await _proxy_authenticated_owner_http(request)
    db = _open_session_db_for_profile(body.profile)
    try:
        sid = db.resolve_session_id(session_id)
        if not sid:
            raise HTTPException(status_code=404, detail="Session not found")
        if body.title is None and body.archived is None:
            raise HTTPException(
                status_code=400,
                detail="Nothing to update; provide 'title' and/or 'archived'.",
            )
        if body.title is not None:
            try:
                db.set_session_title(sid, body.title or "")
            except ValueError as e:
                # Title too long, invalid characters, or already in use.
                raise HTTPException(status_code=400, detail=str(e))
        if body.archived is not None:
            db.set_session_archived(sid, body.archived)
        result = {"ok": True, "title": db.get_session_title(sid) or ""}
        if body.archived is not None:
            result["archived"] = bool(body.archived)
        return result
    finally:
        db.close()


@app.get("/api/sessions/{session_id}/export")
async def export_session_endpoint(request: Request, session_id: str, profile: Optional[str] = None):
    """Export a single session (metadata + messages) as JSON."""
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_session_reader_http(request)
    db = _open_session_db_for_profile(profile)
    try:
        sid = db.resolve_session_id(session_id)
        if not sid:
            raise HTTPException(status_code=404, detail="Session not found")
        data = db.export_session(sid)
        if data is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return data
    finally:
        db.close()


class SessionPrune(BaseModel):
    older_than_days: int = 90
    source: Optional[str] = None
    profile: Optional[str] = None


@app.post("/api/sessions/prune")
async def prune_sessions_endpoint(request: Request, body: SessionPrune):
    """Delete ended sessions older than N days (mirrors `hermes sessions prune`)."""
    if body.older_than_days < 1:
        raise HTTPException(status_code=400, detail="older_than_days must be >= 1")
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(body.profile)
        return await _proxy_authenticated_owner_http(request)
    profile_home = _cron_profile_home(body.profile)[1] if body.profile else get_hermes_home()
    db = _open_session_db_for_profile(body.profile)
    try:
        sessions_dir = profile_home / "sessions"
        removed = db.prune_sessions(
            older_than_days=body.older_than_days,
            source=(body.source or None),
            sessions_dir=sessions_dir if sessions_dir.exists() else None,
        )
        return {"ok": True, "removed": removed}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Log viewer endpoint
# ---------------------------------------------------------------------------


@app.get("/api/logs")
async def get_logs(
    request: Request,
    file: str = "agent",
    lines: int = 100,
    level: Optional[str] = None,
    component: Optional[str] = None,
    search: Optional[str] = None,
):
    if _authenticated_owner_request(request):
        return await _proxy_authenticated_owner_http(request)

    from hermes_cli.logs import log_viewer_payload

    try:
        return log_viewer_payload(
            file=file,
            lines=lines,
            level=level,
            component=component,
            search=search,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Core cron fire webhook and legacy local-profile resolution helpers
# ---------------------------------------------------------------------------


def _cron_profile_home(profile: Optional[str]) -> Tuple[str, Path]:
    """Resolve a local Dashboard profile to its Hermes home."""
    from hermes_cli.cron_dashboard import profile_home

    try:
        return profile_home(profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _find_cron_job_profile(job_id: str) -> Optional[str]:
    from hermes_cli.cron_dashboard import find_job_profile

    return find_job_profile(job_id)


def _fire_cron_job_for_profile(profile: str, job_id: str) -> bool:
    """Run ONE due cron job end-to-end for ``profile`` via the resolved
    scheduler provider's ``fire_due`` (store CAS claim + ``run_one_job``).

    Retargets the ``cron.jobs`` module globals to the profile's cron dir under
    the shared cron-home lock — so the
    claim and the run operate on the right profile's ``jobs.json``. Runs with
    no live adapters; delivery falls back to the per-platform send path (the
    dashboard process has no gateway adapter handles, exactly like the desktop
    cron path above).
    """
    _profile_name, home = _cron_profile_home(profile)
    from cron.jobs import CronStore, use_store

    with use_store(CronStore(home)):
        from cron.scheduler_provider import resolve_cron_scheduler

        provider = resolve_cron_scheduler()
        return bool(provider.fire_due(job_id, adapters=None, loop=None))


@app.post("/api/cron/fire")
async def cron_fire_webhook(request: Request):
    """Chronos managed-cron fire webhook (NAS -> agent).

    Authenticated by a short-lived NAS-minted JWT (verified by the pluggable
    Chronos fire-verifier), NOT the dashboard session cookie — so this path is
    in ``PUBLIC_API_PATHS`` to bypass the dashboard auth gate, and the JWT is
    the real gate. This is the inbound half of scale-to-zero managed cron: NAS
    POSTs here at fire time, the agent verifies, claims the job (store CAS, so
    at-most-once across replicas / on a NAS retry), runs it, and re-arms the
    next one-shot.

    Lives on the dashboard app (not the api_server adapter) because the
    dashboard is the agent's always-reachable public HTTP surface on hosted
    deployments; the gateway may be idle/scaled down.

    Returns 202 immediately and runs the job in the background so a long agent
    turn never trips NAS's HTTP timeout.
    """
    from plugins.cron_providers.chronos.verify import get_fire_verifier

    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.startswith("Bearer ") else ""

    cfg = load_config()
    claims = get_fire_verifier()(
        token=token,
        expected_audience=cfg_get(cfg, "cron", "chronos", "expected_audience", default=""),
        jwks_or_key=cfg_get(cfg, "cron", "chronos", "nas_jwks_url", default="") or None,
        issuer=cfg_get(cfg, "cron", "chronos", "portal_url", default="") or None,
    )
    if claims is None:
        return JSONResponse({"error": "invalid fire token"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        body = {}
    job_id = (body or {}).get("job_id") if isinstance(body, dict) else None
    if not job_id:
        return JSONResponse({"error": "missing job_id"}, status_code=400)

    if ":" in str(job_id):
        owner_key, local_job_id = str(job_id).split(":", 1)
        if not owner_key.startswith("ok1_") or not local_job_id:
            return JSONResponse({"error": "invalid owner job_id"}, status_code=400)
        from hermes_cli.owner_worker.cron_dispatcher import dispatch_owner_job

        supervisor = getattr(request.app.state, "owner_worker_supervisor", None)
        if supervisor is None:
            return JSONResponse({"error": "owner worker unavailable"}, status_code=503)
        asyncio.create_task(
            asyncio.to_thread(
                dispatch_owner_job,
                supervisor,
                get_hermes_home(),
                owner_key,
                local_job_id,
            )
        )
        return JSONResponse(
            {"status": "accepted", "job_id": job_id}, status_code=202
        )

    profile = _find_cron_job_profile(job_id)
    if not profile:
        # Job is gone (cancelled / completed) — nothing to fire. 200 so NAS
        # does not retry a fire that is intentionally absent.
        return JSONResponse({"status": "gone", "job_id": job_id}, status_code=200)

    # Run in the background; the store CAS claim inside fire_due de-dupes a
    # NAS/scheduler retry that arrives while this is in flight.
    asyncio.create_task(
        asyncio.to_thread(_fire_cron_job_for_profile, profile, job_id)
    )
    return JSONResponse({"status": "accepted", "job_id": job_id}, status_code=202)


# ---------------------------------------------------------------------------
# MCP server endpoints — list / add / remove / test.
#
# Wraps the same config data layer the CLI uses (hermes_cli.mcp_config), so
# servers managed here show up under `hermes mcp list` and vice versa.  Secrets
# in stdio `env` blocks are redacted on read; the agent picks them up from
# config.yaml at session start exactly as with CLI-added servers.
# ---------------------------------------------------------------------------


class MCPServerCreate(BaseModel):
    name: str
    url: Optional[str] = None
    command: Optional[str] = None
    args: List[str] = []
    # env: KEY=VALUE map for stdio servers (API keys, etc.)
    env: Dict[str, str] = {}
    # auth: "oauth" | "header" | None
    auth: Optional[str] = None
    profile: Optional[str] = None


def _redact_mcp_env(env: Dict[str, Any]) -> Dict[str, str]:
    """Mask secret-shaped MCP env values for read responses."""
    out: Dict[str, str] = {}
    for k, v in (env or {}).items():
        try:
            out[str(k)] = redact_key(str(v)) if v else ""
        except Exception:
            out[str(k)] = "***"
    return out


def _mcp_server_summary(name: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    transport = "http" if cfg.get("url") else ("stdio" if cfg.get("command") else "unknown")
    return {
        "name": name,
        "transport": transport,
        "url": cfg.get("url"),
        "command": cfg.get("command"),
        "args": list(cfg.get("args") or []),
        "env": _redact_mcp_env(cfg.get("env") or {}),
        "auth": cfg.get("auth"),
        "enabled": cfg.get("enabled", True) is not False,
        # Tool selection: list of enabled tool names, or None = all.
        "tools": cfg.get("tools"),
    }


@app.get("/api/mcp/servers")
async def list_mcp_servers(profile: Optional[str] = None):
    from hermes_cli.mcp_config import _get_mcp_servers

    with _profile_scope(profile):
        servers = _get_mcp_servers()
    return {
        "servers": [
            _mcp_server_summary(name, cfg) for name, cfg in sorted(servers.items())
        ]
    }


@app.post("/api/mcp/servers")
async def add_mcp_server(body: MCPServerCreate, profile: Optional[str] = None):
    from hermes_cli.mcp_config import _get_mcp_servers, _save_mcp_server

    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Server name is required")
    with _profile_scope(body.profile or profile):
        existing = _get_mcp_servers()
    if name in existing:
        raise HTTPException(status_code=409, detail=f"Server '{name}' already exists")
    if not body.url and not body.command:
        raise HTTPException(
            status_code=400,
            detail="Provide either a URL (HTTP/SSE server) or a command (stdio server)",
        )

    server_config: Dict[str, Any] = {}
    if body.url:
        server_config["url"] = body.url.strip()
    if body.command:
        server_config["command"] = body.command.strip()
        if body.args:
            server_config["args"] = list(body.args)
    if body.env:
        server_config["env"] = dict(body.env)
    if body.auth:
        server_config["auth"] = body.auth

    try:
        with _profile_scope(body.profile or profile):
            if not _save_mcp_server(name, server_config):
                raise HTTPException(
                    status_code=400,
                    detail=f"Server '{name}' rejected: suspicious command/args configuration",
                )
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("POST /api/mcp/servers failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _mcp_server_summary(name, server_config)


@app.delete("/api/mcp/servers/{name}")
async def remove_mcp_server(name: str, profile: Optional[str] = None):
    from hermes_cli.mcp_config import _remove_mcp_server

    with _profile_scope(profile):
        removed = _remove_mcp_server(name)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
    return {"ok": True}


@app.post("/api/mcp/servers/{name}/test")
async def test_mcp_server(name: str, profile: Optional[str] = None):
    """Connect to the server, list its tools, disconnect.  Returns tool list."""
    from hermes_cli.mcp_config import _get_mcp_servers, _probe_single_server

    with _profile_scope(profile):
        servers = _get_mcp_servers()
    if name not in servers:
        raise HTTPException(status_code=404, detail=f"Server '{name}' not found")

    def _probe_scoped():
        # Re-enter the scope INSIDE the worker thread so call-time
        # resolution during the probe — env-placeholder expansion in
        # _resolve_mcp_server_config reading the profile's .env — sees the
        # selected profile, matching the config the server was saved into.
        # (asyncio.to_thread copies contextvars, but entering explicitly
        # keeps the lock-protected SKILLS_DIR swap balanced per-thread.)
        # The probe's dedicated MCP event-loop thread is covered too:
        # _run_on_mcp_loop wraps scheduled coroutines with the caller's
        # HERMES_HOME override (see mcp_tool._wrap_with_home_override), so
        # OAuth token stores resolve against the selected profile as well.
        with _profile_scope(profile):
            return _probe_single_server(name, servers[name])

    try:
        # Probe blocks on a dedicated MCP event loop — run in a thread so the
        # FastAPI event loop is never blocked.
        tools = await asyncio.to_thread(_probe_scoped)
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "tools": [],
        }
    return {
        "ok": True,
        "tools": [{"name": t, "description": d} for t, d in tools],
    }


class MCPEnabledToggle(BaseModel):
    enabled: bool
    profile: Optional[str] = None


@app.put("/api/mcp/servers/{name}/enabled")
async def set_mcp_server_enabled(
    name: str, body: MCPEnabledToggle, profile: Optional[str] = None
):
    """Enable or disable an MCP server (takes effect on next session/gateway).

    Toggles the ``enabled`` key on the server's config.yaml entry — the same
    flag the agent reads at startup.  Disabled servers stay in config so they
    can be re-enabled without re-entering their settings.
    """
    with _profile_scope(body.profile or profile):
        cfg = load_config()
        servers = cfg.get("mcp_servers")
        if not isinstance(servers, dict) or name not in servers:
            raise HTTPException(status_code=404, detail=f"Server '{name}' not found")
        if not isinstance(servers[name], dict):
            raise HTTPException(status_code=400, detail="Malformed server config")
        servers[name]["enabled"] = bool(body.enabled)
        save_config(cfg)
    return {"ok": True, "name": name, "enabled": bool(body.enabled)}


@app.get("/api/mcp/catalog")
async def list_mcp_catalog(profile: Optional[str] = None):
    """Browse the Nous-approved MCP catalog (the optional-mcps/ manifests).

    Each entry reports whether it's already installed and enabled so the UI
    can show install / enabled state inline.  This is the same catalog
    `hermes mcp catalog` / `hermes mcp install` read.  ``profile`` scopes
    the installed/enabled annotations (the catalog itself is repo-shipped
    and identical for every profile).
    """
    try:
        from hermes_cli import mcp_catalog
    except Exception as exc:
        _log.exception("mcp_catalog import failed")
        raise HTTPException(status_code=500, detail=f"Catalog unavailable: {exc}")

    entries = []
    try:
        with _profile_scope(profile):
            catalog_entries = list(mcp_catalog.list_catalog())
            installed_state = {
                e.name: (mcp_catalog.is_installed(e.name), mcp_catalog.is_enabled(e.name))
                for e in catalog_entries
            }
        for entry in catalog_entries:
            auth = entry.auth
            transport = entry.transport
            install = entry.install
            entries.append({
                "name": entry.name,
                "description": entry.description,
                "source": entry.source,
                "transport": transport.type,
                "auth_type": getattr(auth, "type", "none"),
                # Env vars the user must supply (names + prompts only, never values).
                "required_env": [
                    {"name": e.name, "prompt": e.prompt, "required": e.required}
                    for e in getattr(auth, "env", []) or []
                ],
                # Transport details so the UI can show exactly what connects/runs.
                # The trust model (docs: user-guide/features/mcp) tells users to
                # inspect command/args/url and the install bootstrap before
                # installing — surface them rather than hiding them in the repo.
                "command": transport.command,
                "args": list(transport.args or []),
                "url": transport.url,
                # Git bootstrap (present only for entries that clone + build).
                "install_url": install.url if install else None,
                "install_ref": install.ref if install else None,
                "bootstrap": list(install.bootstrap) if install else [],
                # Default tool pre-selection hint and post-install guidance.
                "default_enabled": list(entry.tools.default_enabled)
                if entry.tools.default_enabled is not None
                else None,
                "post_install": entry.post_install or "",
                "needs_install": entry.install is not None,
                "installed": installed_state.get(entry.name, (False, False))[0],
                "enabled": installed_state.get(entry.name, (False, False))[1],
            })
    except HTTPException:
        # Unknown/invalid profile → 404, not a silently-empty catalog.
        raise
    except Exception:
        _log.exception("list_mcp_catalog failed")

    diagnostics = []
    try:
        diagnostics = [
            {"name": n, "kind": k, "message": m}
            for (n, k, m) in mcp_catalog.catalog_diagnostics()
        ]
    except Exception:
        pass

    return {"entries": entries, "diagnostics": diagnostics}


class MCPCatalogInstall(BaseModel):
    name: str
    # env: KEY=VALUE map for catalog entries that declare required env vars.
    env: Dict[str, str] = {}
    enable: bool = True
    profile: Optional[str] = None


@app.post("/api/mcp/catalog/install")
async def install_mcp_catalog_entry(body: MCPCatalogInstall, profile: Optional[str] = None):
    """Install a catalog MCP into config.yaml.

    For HTTP/stdio entries with required env vars, those are written to .env
    via the standard env path so the agent can read them at session start.
    Entries that need a git bootstrap (``needs_install``) are installed via
    the CLI action path because the clone can take time.
    """
    from hermes_cli import mcp_catalog

    name = (body.name or "").strip()
    entry = mcp_catalog.get_entry(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"No catalog entry '{name}'")

    # Persist any supplied env vars first (catalog entries declare which names
    # they need; we only write the ones the user provided).
    effective_profile = body.profile or profile
    if body.env:
        with _profile_scope(effective_profile):
            for k, v in body.env.items():
                if v:
                    save_env_value(k, v)

    # Git-bootstrap entries can take a while to clone — run via the background
    # action path so the request returns immediately and the UI can tail logs.
    # The -p subprocess rebinds HERMES_HOME-derived paths in the child.
    if entry.install is not None:
        try:
            proc = _spawn_hermes_action(
                _profile_cli_args(effective_profile) + ["mcp", "install", name],
                "mcp-install",
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Install failed: {exc}")
        return {"ok": True, "name": name, "background": True, "action": "mcp-install"}

    # No git step — install synchronously via the catalog API. install_entry
    # routes through load_config/save_config + save_env_value, all call-time
    # resolvers, so the context override scopes it. Wrap the to_thread body
    # in the scope INSIDE the thread (contextvars don't propagate into
    # to_thread the other way around — asyncio.to_thread copies context, so
    # setting it here works; keep it explicit for clarity).
    def _install_scoped():
        with _profile_scope(effective_profile):
            mcp_catalog.install_entry(entry, enable=body.enable)

    try:
        await asyncio.to_thread(_install_scoped)
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("install_mcp_catalog_entry failed")
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "name": name, "background": False}


# Register the mcp-install action log so /api/actions/mcp-install/status works.
_ACTION_LOG_FILES.setdefault("mcp-install", "action-mcp-install.log")
_ACTION_LOG_FILES.setdefault("computer-use-grant", "action-computer-use-grant.log")


# ---------------------------------------------------------------------------
# Pairing endpoints — approve / revoke / list messaging pairing codes.
#
# These are how a remote admin onboards messaging users (Telegram, Discord, …)
# without shell access.  Wraps gateway.pairing.PairingStore directly.
# ---------------------------------------------------------------------------


class PairingApprove(BaseModel):
    platform: str
    code: str


class PairingRevoke(BaseModel):
    platform: str
    user_id: str


def _pairing_store():
    from gateway.pairing import PairingStore

    return PairingStore()


@app.get("/api/pairing")
async def list_pairing():
    store = _pairing_store()
    return {
        "pending": store.list_pending(),
        "approved": store.list_approved(),
    }


@app.post("/api/pairing/approve")
async def approve_pairing(body: PairingApprove):
    store = _pairing_store()
    platform = (body.platform or "").lower().strip()
    code = (body.code or "").upper().strip()
    if not platform or not code:
        raise HTTPException(status_code=400, detail="platform and code are required")

    result = store.approve_code(platform, code)
    if result:
        return {"ok": True, "user": result}
    if store._is_locked_out(platform):
        raise HTTPException(
            status_code=429,
            detail=f"Platform '{platform}' is locked out after too many failed approvals.",
        )
    raise HTTPException(
        status_code=404,
        detail=f"Code '{code}' not found or expired for platform '{platform}'.",
    )


@app.post("/api/pairing/revoke")
async def revoke_pairing(body: PairingRevoke):
    store = _pairing_store()
    platform = (body.platform or "").lower().strip()
    if not platform or not body.user_id:
        raise HTTPException(status_code=400, detail="platform and user_id are required")
    if store.revoke(platform, body.user_id):
        return {"ok": True}
    raise HTTPException(
        status_code=404,
        detail=f"User {body.user_id} not found in approved list for {platform}.",
    )


@app.post("/api/pairing/clear-pending")
async def clear_pending_pairing():
    store = _pairing_store()
    count = store.clear_pending()
    return {"ok": True, "cleared": count}


# ---------------------------------------------------------------------------
# Credential pool endpoints — list / add / remove rotation keys.
#
# The credential pool (auth.json -> credential_pool.<provider>[]) holds the
# rotating API keys the agent round-robins through.  Secrets are redacted on
# read; only the agent ever sees the raw values at session start.
# ---------------------------------------------------------------------------


class CredentialPoolAdd(BaseModel):
    provider: str
    # api_key for API-key providers; OAuth pooling stays CLI-only (it needs
    # an interactive browser flow that doesn't belong in a single POST).
    api_key: str
    label: Optional[str] = None


def _pool_entry_summary(entry: Any, index: int) -> Dict[str, Any]:
    """Redacted, display-safe view of one PooledCredential.

    ``index`` is 1-based to match CredentialPool.remove_index().
    """
    token = getattr(entry, "access_token", "") or ""
    return {
        "index": index,
        "id": getattr(entry, "id", None),
        "label": getattr(entry, "label", None),
        "auth_type": getattr(entry, "auth_type", None),
        "source": getattr(entry, "source", None),
        "priority": getattr(entry, "priority", 0),
        "last_status": getattr(entry, "last_status", None),
        "request_count": getattr(entry, "request_count", 0),
        "token_preview": redact_key(token) if token else "",
        "has_refresh": bool(getattr(entry, "refresh_token", None)),
    }


@app.get("/api/credentials/pool")
async def list_credential_pool():
    from agent.credential_pool import load_pool
    from hermes_cli.auth import read_credential_pool

    providers = []
    # read_credential_pool(None) lists every provider that has pooled entries;
    # load_pool() then gives us the rich PooledCredential objects per provider.
    raw_pool = read_credential_pool()
    for provider_id in sorted(raw_pool.keys()):
        try:
            pool = load_pool(provider_id)
        except Exception:
            _log.exception("load_pool(%s) failed", provider_id)
            continue
        entries = pool.entries()
        if not entries:
            continue
        providers.append({
            "provider": provider_id,
            "entries": [
                _pool_entry_summary(e, i) for i, e in enumerate(entries, start=1)
            ],
        })
    return {"providers": providers}


@app.post("/api/credentials/pool")
async def add_credential_pool_entry(body: CredentialPoolAdd):
    import uuid as _uuid
    from agent.credential_pool import (
        load_pool,
        PooledCredential,
        AUTH_TYPE_API_KEY,
        SOURCE_MANUAL,
    )

    provider = (body.provider or "").strip().lower()
    api_key = (body.api_key or "").strip()
    if not provider or not api_key:
        raise HTTPException(status_code=400, detail="provider and api_key are required")

    try:
        pool = load_pool(provider)
        label = (body.label or "").strip() or f"key #{len(pool.entries()) + 1}"
        entry = PooledCredential(
            provider=provider,
            id=_uuid.uuid4().hex[:6],
            label=label,
            auth_type=AUTH_TYPE_API_KEY,
            priority=0,
            source=SOURCE_MANUAL,
            access_token=api_key,
        )
        pool.add_entry(entry)
    except Exception as exc:
        _log.exception("POST /api/credentials/pool failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "provider": provider, "count": len(pool.entries())}


@app.delete("/api/credentials/pool/{provider}/{index}")
async def remove_credential_pool_entry(provider: str, index: int):
    """Remove a pool entry.  ``index`` is 1-based (matches the list response)."""
    from agent.credential_pool import load_pool

    provider = (provider or "").strip().lower()
    try:
        pool = load_pool(provider)
        removed = pool.remove_index(index)
    except Exception as exc:
        _log.exception("DELETE /api/credentials/pool failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if removed is None:
        raise HTTPException(status_code=404, detail="No pool entry at that index")
    return {"ok": True, "provider": provider, "count": len(pool.entries())}


# ---------------------------------------------------------------------------
# Memory provider endpoints — status / list providers / select / disable / reset.
#
# Selecting a provider only writes config.memory.provider (full interactive
# provider setup, with its API-key prompts, stays on the CLI via
# `hermes memory setup`).  The dashboard covers the common admin actions:
# see which provider is active, switch the built-in store on/off, and wipe
# built-in memory files.
# ---------------------------------------------------------------------------


class MemoryProviderSelect(BaseModel):
    # "" or "built-in" disables the external provider (built-in only).
    provider: str


class MemoryReset(BaseModel):
    # "all" | "memory" | "user"
    target: str = "all"


@app.get("/api/memory")
async def get_memory_status():
    from plugins.memory import discover_memory_providers

    cfg = load_config()
    active = ""
    mem = cfg.get("memory")
    if isinstance(mem, dict):
        active = str(mem.get("provider") or "")

    providers = []
    try:
        for name, description, configured in discover_memory_providers():
            providers.append({
                "name": name,
                "description": description,
                "configured": bool(configured),
            })
    except Exception:
        _log.exception("discover_memory_providers failed")

    # Built-in memory file sizes (so the UI can show what a reset would erase).
    mem_dir = get_hermes_home() / "memories"
    files = {}
    for fname, key in (("MEMORY.md", "memory"), ("USER.md", "user")):
        path = mem_dir / fname
        files[key] = path.stat().st_size if path.exists() else 0

    return {
        "active": active,
        "providers": providers,
        "builtin_files": files,
    }


@app.put("/api/memory/provider")
async def set_memory_provider(body: MemoryProviderSelect):
    provider = (body.provider or "").strip()
    if provider.lower() in {"built-in", "builtin", "none"}:
        provider = ""

    if provider:
        from plugins.memory import discover_memory_providers

        valid = {name for name, _d, _c in discover_memory_providers()}
        if provider not in valid:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown memory provider '{provider}'. Run `hermes memory setup` to configure a new one.",
            )

    cfg = load_config()
    if not isinstance(cfg.get("memory"), dict):
        cfg["memory"] = {}
    cfg["memory"]["provider"] = provider
    save_config(cfg)
    return {"ok": True, "active": provider}


@app.post("/api/memory/reset")
async def reset_memory(body: MemoryReset):
    target = (body.target or "all").strip().lower()
    if target not in {"all", "memory", "user"}:
        raise HTTPException(status_code=400, detail="target must be all, memory, or user")

    mem_dir = get_hermes_home() / "memories"
    deleted = []
    targets = []
    if target in {"all", "memory"}:
        targets.append("MEMORY.md")
    if target in {"all", "user"}:
        targets.append("USER.md")
    for fname in targets:
        path = mem_dir / fname
        if path.exists():
            try:
                path.unlink()
                deleted.append(fname)
            except OSError as exc:
                raise HTTPException(status_code=500, detail=f"Could not delete {fname}: {exc}")
    return {"ok": True, "deleted": deleted}


# ---------------------------------------------------------------------------
# Operations endpoints — doctor / security audit / backup / import /
# checkpoints / hooks.
#
# Diagnostic and maintenance commands.  The long-running / text-output ones
# (doctor, security audit, backup, import, skills install) are spawned as
# background actions whose logs the dashboard tails via
# /api/actions/{name}/status — same pattern as gateway restart and update.
# The cheap, structured reads (hooks list, checkpoints list) return JSON
# directly.
# ---------------------------------------------------------------------------


@app.post("/api/ops/doctor")
async def run_doctor():
    try:
        proc = _spawn_hermes_action(["doctor"], "doctor")
    except Exception as exc:
        _log.exception("Failed to spawn doctor")
        raise HTTPException(status_code=500, detail=f"Failed to run doctor: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "doctor"}


@app.post("/api/ops/security-audit")
async def run_security_audit():
    try:
        proc = _spawn_hermes_action(["security", "audit"], "security-audit")
    except Exception as exc:
        _log.exception("Failed to spawn security audit")
        raise HTTPException(status_code=500, detail=f"Failed to run security audit: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "security-audit"}


class BackupRequest(BaseModel):
    # Optional output path; defaults to a timestamped zip in the home dir.
    output: Optional[str] = None


def _dashboard_backup_dir() -> Path:
    return get_hermes_home() / "backups"


def _new_dashboard_backup_path() -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    return _dashboard_backup_dir() / f"hermes-backup-{stamp}-{secrets.token_hex(4)}.zip"


@app.post("/api/ops/backup")
async def run_backup(body: BackupRequest):
    args = ["backup"]
    archive: Optional[Path] = None
    if body.output:
        args.append(body.output.strip())
    else:
        archive = _new_dashboard_backup_path()
        try:
            archive.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Could not create backup directory: {exc}",
            )
        args.append(str(archive))
    try:
        proc = _spawn_hermes_action(args, "backup")
    except Exception as exc:
        _log.exception("Failed to spawn backup")
        raise HTTPException(status_code=500, detail=f"Failed to run backup: {exc}")
    response = {"ok": True, "pid": proc.pid, "name": "backup"}
    if archive is not None:
        response["archive"] = str(archive)
    return response


@app.get("/api/ops/backup/download")
async def download_dashboard_backup(archive: str):
    try:
        backup_dir = _dashboard_backup_dir().expanduser().resolve(strict=False)
        target = Path(archive).expanduser().resolve(strict=True)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Backup not found")
    except (OSError, RuntimeError):
        raise HTTPException(status_code=400, detail="Invalid backup path")

    if not _path_is_under(backup_dir, target):
        raise HTTPException(status_code=403, detail="Backup is outside the dashboard backup directory")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="Backup not found")

    return FileResponse(
        path=str(target),
        media_type="application/zip",
        filename=target.name,
        content_disposition_type="attachment",
    )


class ImportRequest(BaseModel):
    archive: str
    # Pass --force to `hermes import`. The spawned action runs with
    # stdin=DEVNULL, so the CLI's interactive "Continue? [y/N]" overwrite
    # prompt hits EOF and auto-aborts ("Aborted.", exit 1) whenever the
    # target already has a config — which it always does when the dashboard
    # itself is running from it. The dashboard shows its own confirm modal
    # before calling this endpoint, then sends force=True so the restore
    # proceeds non-interactively.
    force: bool = False


@app.post("/api/ops/import")
async def run_import(body: ImportRequest):
    archive = (body.archive or "").strip()
    if not archive:
        raise HTTPException(status_code=400, detail="archive path is required")
    if not os.path.isfile(archive):
        raise HTTPException(status_code=404, detail=f"Archive not found: {archive}")
    args = ["import", archive]
    if body.force:
        args.append("--force")
    try:
        proc = _spawn_hermes_action(args, "import")
    except Exception as exc:
        _log.exception("Failed to spawn import")
        raise HTTPException(status_code=500, detail=f"Failed to run import: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "import"}


def _safe_backup_upload_name(filename: str | None) -> str:
    name = Path(filename or "backup.zip").name.strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-")
    if not name:
        name = "backup.zip"
    if not name.lower().endswith(".zip"):
        name = f"{name}.zip"
    return name


@app.post("/api/ops/import-upload")
async def run_import_upload(
    file: UploadFile = File(...),
    force: bool = Form(False),
):
    staging_dir = _dashboard_backup_dir()
    try:
        staging_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not create import staging directory: {exc}",
        )

    safe_name = _safe_backup_upload_name(file.filename)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = staging_dir / f"dashboard-import-{stamp}-{secrets.token_hex(4)}-{safe_name}"
    tmp_fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".upload",
        dir=str(staging_dir),
    )
    tmp_path = Path(tmp_name)
    total = 0
    renamed = False
    try:
        with os.fdopen(tmp_fd, "wb") as out:
            while True:
                chunk = await file.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MANAGED_FILE_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="Archive is too large")
                out.write(chunk)
        os.replace(tmp_path, target)
        renamed = True
    except HTTPException:
        raise
    except PermissionError:
        raise HTTPException(
            status_code=403,
            detail="Import staging directory is not writable",
        )
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not write uploaded archive: {exc}",
        )
    finally:
        if not renamed:
            tmp_path.unlink(missing_ok=True)
        await file.close()

    if not zipfile.is_zipfile(target):
        target.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail="Uploaded archive is not a valid zip file",
        )

    args = ["import", str(target)]
    if force:
        args.append("--force")
    try:
        proc = _spawn_hermes_action(args, "import")
    except Exception as exc:
        _log.exception("Failed to spawn import")
        raise HTTPException(status_code=500, detail=f"Failed to run import: {exc}")
    return {
        "ok": True,
        "pid": proc.pid,
        "name": "import",
        "archive": str(target),
        "uploaded_bytes": total,
    }


@app.get("/api/ops/hooks")
async def list_hooks():
    """List configured shell hooks from config.yaml with consent + health.

    Reports each hook's allowlist (consent) status and whether the script is
    currently executable, plus the set of valid hook events so the create
    form can offer them.
    """
    from hermes_cli.config import load_config as _load_config
    from agent import shell_hooks

    try:
        from hermes_cli.plugins import VALID_HOOKS
        valid_events = sorted(VALID_HOOKS)
    except Exception:
        valid_events = []

    specs = []
    try:
        specs = shell_hooks.iter_configured_hooks(_load_config())
    except Exception:
        _log.exception("iter_configured_hooks failed")

    out = []
    for spec in specs:
        entry = None
        try:
            entry = shell_hooks.allowlist_entry_for(spec.event, spec.command)
        except Exception:
            pass
        executable = False
        try:
            executable = shell_hooks.script_is_executable(spec.command)
        except Exception:
            pass
        out.append({
            "event": spec.event,
            "matcher": spec.matcher,
            "command": spec.command,
            "timeout": spec.timeout,
            "allowed": entry is not None,
            "approved_at": (entry or {}).get("approved_at"),
            "executable": executable,
        })

    return {"hooks": out, "valid_events": valid_events}


class HookCreate(BaseModel):
    event: str
    command: str
    matcher: Optional[str] = None
    timeout: Optional[int] = None
    # approve: write the consent allowlist entry too (the operator using the
    # authenticated dashboard is giving consent). Without it the hook is
    # configured but won't fire until approved.
    approve: bool = True


@app.post("/api/ops/hooks")
async def create_hook(body: HookCreate):
    """Add a shell hook to config.yaml (and optionally approve it).

    Shell hooks run arbitrary commands, so this is a privileged action: it
    writes to the ``hooks:`` config block and, when ``approve`` is set, records
    consent in the allowlist so the hook actually fires.  Takes effect on the
    next session / gateway restart.
    """
    from agent import shell_hooks

    event = (body.event or "").strip()
    command = (body.command or "").strip()
    if not event or not command:
        raise HTTPException(status_code=400, detail="event and command are required")

    try:
        from hermes_cli.plugins import VALID_HOOKS
        if event not in VALID_HOOKS:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown event '{event}'. Valid: {', '.join(sorted(VALID_HOOKS))}",
            )
    except HTTPException:
        raise
    except Exception:
        pass

    cfg = load_config()
    hooks_cfg = cfg.get("hooks")
    if not isinstance(hooks_cfg, dict):
        hooks_cfg = {}
        cfg["hooks"] = hooks_cfg
    entries = hooks_cfg.get(event)
    if not isinstance(entries, list):
        entries = []
        hooks_cfg[event] = entries

    new_entry: Dict[str, Any] = {"command": command}
    if body.matcher:
        new_entry["matcher"] = body.matcher
    if body.timeout is not None:
        new_entry["timeout"] = int(body.timeout)
    entries.append(new_entry)
    save_config(cfg)

    approved = False
    if body.approve:
        try:
            shell_hooks._record_approval(event, command)
            approved = True
        except Exception:
            _log.exception("hook consent record failed")

    return {"ok": True, "event": event, "command": command, "approved": approved}


class HookDelete(BaseModel):
    event: str
    command: str


@app.delete("/api/ops/hooks")
async def delete_hook(body: HookDelete):
    """Remove a hook from config.yaml and revoke its consent allowlist entry."""
    from agent import shell_hooks

    event = (body.event or "").strip()
    command = (body.command or "").strip()
    if not event or not command:
        raise HTTPException(status_code=400, detail="event and command are required")

    cfg = load_config()
    hooks_cfg = cfg.get("hooks")
    removed = False
    if isinstance(hooks_cfg, dict) and isinstance(hooks_cfg.get(event), list):
        before = len(hooks_cfg[event])
        hooks_cfg[event] = [
            e for e in hooks_cfg[event]
            if not (isinstance(e, dict) and e.get("command") == command)
        ]
        removed = len(hooks_cfg[event]) < before
        if not hooks_cfg[event]:
            del hooks_cfg[event]
        if not hooks_cfg:
            cfg.pop("hooks", None)
        save_config(cfg)

    # Revoke consent regardless so a re-add re-prompts.
    try:
        shell_hooks.revoke(command)
    except Exception:
        pass

    if not removed:
        raise HTTPException(status_code=404, detail="No matching hook found")
    return {"ok": True}


@app.get("/api/ops/checkpoints")
async def list_checkpoints():
    """List the /rollback shadow store checkpoints (read-only)."""
    # Checkpoints live under <hermes_home>/checkpoints/.  Surface a count +
    # total size so the dashboard can show what a prune would reclaim; the
    # actual prune is a spawned action so confirmation/pruning logic stays
    # in one place (the CLI).
    cp_dir = get_hermes_home() / "checkpoints"
    sessions = []
    total_bytes = 0
    if cp_dir.is_dir():
        for child in sorted(cp_dir.iterdir()):
            if not child.is_dir():
                continue
            size = 0
            count = 0
            for f in child.rglob("*"):
                if f.is_file():
                    try:
                        size += f.stat().st_size
                        count += 1
                    except OSError:
                        pass
            total_bytes += size
            sessions.append({
                "session": child.name,
                "files": count,
                "bytes": size,
            })
    return {"sessions": sessions, "total_bytes": total_bytes}


@app.post("/api/ops/checkpoints/prune")
async def prune_checkpoints():
    try:
        proc = _spawn_hermes_action(["checkpoints", "prune"], "checkpoints-prune")
    except Exception as exc:
        _log.exception("Failed to spawn checkpoints prune")
        raise HTTPException(status_code=500, detail=f"Failed to prune checkpoints: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "checkpoints-prune"}


# ---------------------------------------------------------------------------
# Skills hub endpoints — search / install / uninstall / update.
#
# Search and install touch the network (GitHub, hub sources) and run the same
# complex source-router pipeline the CLI uses, so they're spawned as background
# actions whose logs the dashboard tails.  The already-installed skill list +
# enable/disable toggle live in the existing /api/skills endpoints.
# ---------------------------------------------------------------------------


class SkillInstallRequest(BaseModel):
    identifier: str
    profile: Optional[str] = None


def _profile_cli_args(profile: Optional[str]) -> List[str]:
    """Return ``["-p", <name>]`` for a validated non-default profile.

    Hub install/uninstall/update run in a fresh ``hermes`` subprocess, and
    ``_apply_profile_override()`` reads ``-p`` from argv in the child — the
    only mechanism that reaches import-time-bound globals like
    ``skills_hub.SKILLS_DIR``. Empty/"current" means the dashboard's own
    profile (no args, legacy behavior).
    """
    requested = (profile or "").strip()
    if not requested or requested.lower() in {"current", "default"}:
        return []
    from hermes_cli import profiles as profiles_mod
    _resolve_profile_dir(requested)
    return ["-p", profiles_mod.normalize_profile_name(requested)]


@app.post("/api/skills/hub/install")
async def install_skill_hub(body: SkillInstallRequest, profile: Optional[str] = None):
    identifier = (body.identifier or "").strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="identifier is required")
    try:
        proc = _spawn_hermes_action(
            _profile_cli_args(body.profile or profile)
            + ["skills", "install", identifier, "--yes"],
            "skills-install",
        )
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("Failed to spawn skills install")
        raise HTTPException(status_code=500, detail=f"Failed to install skill: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "skills-install"}


class SkillUninstallRequest(BaseModel):
    name: str
    profile: Optional[str] = None


@app.post("/api/skills/hub/uninstall")
async def uninstall_skill_hub(body: SkillUninstallRequest, profile: Optional[str] = None):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    try:
        proc = _spawn_hermes_action(
            _profile_cli_args(body.profile or profile) + ["skills", "uninstall", name, "--yes"],
            "skills-uninstall",
        )
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("Failed to spawn skills uninstall")
        raise HTTPException(status_code=500, detail=f"Failed to uninstall skill: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "skills-uninstall"}


class SkillsUpdateRequest(BaseModel):
    profile: Optional[str] = None


@app.post("/api/skills/hub/update")
async def update_skills_hub(
    body: Optional[SkillsUpdateRequest] = None, profile: Optional[str] = None
):
    try:
        effective = (body.profile if body else None) or profile
        proc = _spawn_hermes_action(
            _profile_cli_args(effective) + ["skills", "update"], "skills-update"
        )
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("Failed to spawn skills update")
        raise HTTPException(status_code=500, detail=f"Failed to update skills: {exc}")
    return {"ok": True, "pid": proc.pid, "name": "skills-update"}


# Human-readable labels for each hub source id (matches `hermes skills search`
# provenance).  Keep in sync with create_source_router()'s source list.
_SKILL_HUB_SOURCE_LABELS = {
    "official": "Official (Nous)",
    "hermes-index": "Hermes Index",
    "skills-sh": "skills.sh",
    "well-known": "Well-Known",
    "url": "Direct URL",
    "github": "GitHub",
    "clawhub": "ClawHub",
    "claude-marketplace": "Claude Marketplace",
    "lobehub": "LobeHub",
    "browse-sh": "browse.sh",
}


def _skill_meta_to_payload(m) -> dict:
    return {
        "name": m.name,
        "description": m.description,
        "source": m.source,
        "identifier": m.identifier,
        "trust_level": m.trust_level,
        "repo": m.repo,
        "tags": list(m.tags or []),
    }


def _installed_hub_identifiers(profile: Optional[str] = None) -> dict:
    """Map identifier -> installed lock entry for hub-installed skills.

    Lets the UI mark search results that are already installed.  Scoped to
    ``profile``'s skills/.hub/lock.json when provided (HubLockFile takes an
    explicit path, sidestepping the import-time LOCK_FILE binding).
    Best-effort: returns an empty dict if the lock file can't be read.
    """
    try:
        from tools.skills_hub import HubLockFile

        requested = (profile or "").strip()
        if requested and requested.lower() != "current":
            profile_dir = _resolve_profile_dir(requested)
            lock = HubLockFile(profile_dir / "skills" / ".hub" / "lock.json")
        else:
            lock = HubLockFile()
        out = {}
        for entry in lock.list_installed():
            ident = entry.get("identifier")
            if ident:
                out[ident] = {
                    "name": entry.get("name"),
                    "trust_level": entry.get("trust_level"),
                    "scan_verdict": entry.get("scan_verdict"),
                }
        return out
    except Exception:
        return {}


@app.get("/api/skills/hub/sources")
async def list_skills_hub_sources(profile: Optional[str] = None):
    """List the configured skill-hub sources and installed-skill provenance.

    Gives the dashboard something to show BEFORE a search runs — which hubs
    are wired up, their trust tier, and a set of featured skills pulled from
    the centralized index (zero extra API calls).  Without this the Browse-hub
    tab is a blank page with no indication it's even connected to anything.
    ``profile`` scopes the installed-skill provenance to that profile.
    """

    def _run():
        from tools.skills_hub import create_source_router

        sources = create_source_router()
        out = []
        index_available = False
        featured = []
        for src in sources:
            sid = src.source_id()
            entry = {
                "id": sid,
                "label": _SKILL_HUB_SOURCE_LABELS.get(sid, sid),
            }
            # GitHub exposes a rate-limit flag; the index an availability flag.
            if sid == "github":
                try:
                    entry["rate_limited"] = bool(getattr(src, "is_rate_limited", False))
                except Exception:
                    entry["rate_limited"] = False
            if sid == "hermes-index":
                try:
                    index_available = bool(getattr(src, "is_available", False))
                except Exception:
                    index_available = False
                entry["available"] = index_available
                # Empty-query search on the index returns featured/popular skills.
                if index_available:
                    try:
                        featured = [
                            _skill_meta_to_payload(m) for m in src.search("", limit=12)
                        ]
                    except Exception:
                        featured = []
            out.append(entry)
        return {
            "sources": out,
            "index_available": index_available,
            "featured": featured,
            "installed": _installed_hub_identifiers(profile),
        }

    try:
        return await asyncio.to_thread(_run)
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("skills hub sources listing failed")
        raise HTTPException(status_code=502, detail=f"Hub sources failed: {exc}")


@app.get("/api/skills/hub/search")
async def search_skills_hub(
    q: str = "", source: str = "all", limit: int = 20, profile: Optional[str] = None
):
    """Search the skill hub across all configured sources.

    Network-bound (parallel source search); runs in a thread so the FastAPI
    loop isn't blocked.  Returns structured results the UI installs by
    identifier via POST /api/skills/hub/install, previews via
    /api/skills/hub/preview, and scans via /api/skills/hub/scan.
    """
    query = (q or "").strip()
    if not query:
        return {"results": [], "source_counts": {}, "timed_out": [], "installed": {}}

    def _run():
        from tools.skills_hub import create_source_router, parallel_search_sources

        sources = create_source_router()
        capped = min(max(limit, 1), 50)
        all_results, source_counts, timed_out = parallel_search_sources(
            sources, query=query, source_filter=source or "all", overall_timeout=30
        )

        # Dedupe by identifier, preferring higher trust (mirrors unified_search).
        _rank = {"builtin": 2, "trusted": 1, "community": 0}
        seen = {}
        for r in all_results:
            if r.identifier not in seen:
                seen[r.identifier] = r
            elif _rank.get(r.trust_level, 0) > _rank.get(seen[r.identifier].trust_level, 0):
                seen[r.identifier] = r
        deduped = list(seen.values())[:capped]

        return {
            "results": [_skill_meta_to_payload(m) for m in deduped],
            "source_counts": source_counts,
            "timed_out": timed_out,
            "installed": _installed_hub_identifiers(profile),
        }

    try:
        return await asyncio.to_thread(_run)
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("skills hub search failed")
        raise HTTPException(status_code=502, detail=f"Hub search failed: {exc}")


@app.get("/api/skills/hub/preview")
async def preview_skill_hub(identifier: str = ""):
    """Fetch a hub skill's SKILL.md content + metadata for in-dashboard reading.

    Resolves the identifier across configured sources (same path the CLI
    installer uses), then returns the rendered SKILL.md text and the file
    manifest WITHOUT installing anything.  This is the 'read the actual skill
    before installing' affordance the Browse-hub tab was missing.
    """
    ident = (identifier or "").strip()
    if not ident:
        raise HTTPException(status_code=400, detail="identifier is required")

    def _run():
        from hermes_cli.skills_hub import _resolve_source_meta_and_bundle
        from tools.skills_hub import create_source_router

        sources = create_source_router()
        meta, bundle, _src = _resolve_source_meta_and_bundle(ident, sources)
        if not bundle and not meta:
            return None

        files = {}
        skill_md = ""
        if bundle:
            for rel, content in (bundle.files or {}).items():
                if isinstance(content, bytes):
                    # Some sources (e.g. official optional skills) store every
                    # file as bytes.  Decode text so SKILL.md / docs render;
                    # only fall back to a placeholder for genuinely-binary data.
                    try:
                        files[rel] = content.decode("utf-8")
                    except UnicodeDecodeError:
                        files[rel] = "(binary file)"
                else:
                    files[rel] = content
            skill_md = files.get("SKILL.md", "") or ""

        m = meta or bundle
        return {
            "name": getattr(m, "name", ident),
            "description": getattr(m, "description", "") or "",
            "source": getattr(m, "source", "") or "",
            "identifier": getattr(m, "identifier", ident) or ident,
            "trust_level": getattr(m, "trust_level", "community") or "community",
            "repo": getattr(m, "repo", None),
            "tags": list(getattr(m, "tags", None) or []),
            "skill_md": skill_md,
            "files": sorted(files.keys()),
        }

    try:
        result = await asyncio.to_thread(_run)
    except Exception as exc:
        _log.exception("skills hub preview failed")
        raise HTTPException(status_code=502, detail=f"Hub preview failed: {exc}")
    if result is None:
        raise HTTPException(status_code=404, detail=f"Skill not found: {ident}")
    return result


@app.get("/api/skills/hub/scan")
async def scan_skill_hub(identifier: str = ""):
    """Run the install-time security scan on a hub skill WITHOUT installing it.

    Fetches the bundle, quarantines it, and runs the same `scan_skill` /
    `should_allow_install` pipeline the CLI installer uses — then cleans up the
    quarantine.  Returns the verdict, per-finding detail, trust tier, and the
    install-policy decision so the dashboard can show a visual safety result
    on demand (the 'scan' button the Browse-hub tab was missing).
    """
    ident = (identifier or "").strip()
    if not ident:
        raise HTTPException(status_code=400, detail="identifier is required")

    def _run():
        import shutil as _shutil

        from hermes_cli.skills_hub import _resolve_source_meta_and_bundle
        from tools.skills_hub import create_source_router, quarantine_bundle
        from tools.skills_guard import scan_skill, should_allow_install

        sources = create_source_router()
        meta, bundle, _src = _resolve_source_meta_and_bundle(ident, sources)
        if not bundle:
            return None

        if bundle.source == "official":
            scan_source = "official"
        else:
            scan_source = (
                getattr(bundle, "identifier", "")
                or getattr(meta, "identifier", "")
                or ident
            )

        q_path = None
        try:
            q_path = quarantine_bundle(bundle)
            result = scan_skill(q_path, source=scan_source)
        finally:
            if q_path is not None:
                _shutil.rmtree(q_path, ignore_errors=True)

        allowed, reason = should_allow_install(result, force=False)
        # `allowed` may be None ("ask") for agent-created/dangerous gates.
        if allowed is True:
            policy = "allow"
        elif allowed is None:
            policy = "ask"
        else:
            policy = "block"

        findings = [
            {
                "severity": f.severity,
                "category": f.category,
                "file": f.file,
                "line": f.line,
                "description": f.description,
            }
            for f in result.findings
        ]
        # Per-severity tally for an at-a-glance summary.
        counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for f in result.findings:
            if f.severity in counts:
                counts[f.severity] += 1

        return {
            "name": result.skill_name,
            "identifier": ident,
            "source": result.source,
            "trust_level": result.trust_level,
            "verdict": result.verdict,
            "summary": result.summary,
            "policy": policy,
            "policy_reason": reason,
            "findings": findings,
            "severity_counts": counts,
        }

    try:
        result = await asyncio.to_thread(_run)
    except Exception as exc:
        _log.exception("skills hub scan failed")
        raise HTTPException(status_code=502, detail=f"Hub scan failed: {exc}")
    if result is None:
        raise HTTPException(status_code=404, detail=f"Skill not found: {ident}")
    return result


# ---------------------------------------------------------------------------
# Skills & Tools endpoints
#
# Every read/write below accepts an optional ``profile`` query param so the
# dashboard can manage ANY profile's skills/toolsets, not just the profile
# the dashboard process happens to be running under. Without this, "Set as
# active" on the Profiles page (which only flips the sticky ``active_profile``
# file for FUTURE CLI/gateway invocations) misled users into thinking skill
# toggles would land in the activated profile — they silently wrote into the
# dashboard's own config instead. See _profile_scope() for the mechanism.
# ---------------------------------------------------------------------------


_SKILLS_PROFILE_LOCK = threading.RLock()


@contextmanager
def _profile_scope(profile: Optional[str]):
    """Scope config + skill-directory resolution to ``profile`` for one request.

    Two seams must be redirected for skills/toolsets endpoints:

    1. ``load_config``/``save_config`` resolve ``get_hermes_home()`` at call
       time — the context-local override from ``set_hermes_home_override``
       reaches them (same pattern as ``_write_profile_model``).
    2. ``tools.skills_tool`` and ``tools.skill_manager_tool`` bind
       ``SKILLS_DIR`` at import time, so the override CANNOT reach them.
       Like ``cron.jobs.use_store`` does for cron storage, temporarily
       retarget both under a lock and restore them
       immediately after.

    ``profile`` of None/""/"current" means "the dashboard's own profile" —
    config resolution is untouched, but the skill-module globals are still
    retargeted to the *current* ``get_hermes_home()`` so writes land in the
    live home even when the import-time binding is stale (e.g. the process
    imported the modules before a HERMES_HOME override, or under test
    isolation).
    """
    _reject_authenticated_control_plane_owner_surface("Profile-scoped management APIs")
    requested = (profile or "").strip()

    from hermes_constants import (
        get_hermes_home,
        set_hermes_home_override,
        reset_hermes_home_override,
    )
    from tools import skills_tool as _skills_tool
    from tools import skill_manager_tool as _skill_mgr

    token = None
    if not requested or requested.lower() == "current":
        profile_dir = get_hermes_home()
    else:
        profile_dir = _resolve_profile_dir(requested)
        token = set_hermes_home_override(str(profile_dir))

    with _SKILLS_PROFILE_LOCK:
        old_home = _skills_tool.HERMES_HOME
        old_skills_dir = _skills_tool.SKILLS_DIR
        old_mgr_home = _skill_mgr.HERMES_HOME
        old_mgr_skills_dir = _skill_mgr.SKILLS_DIR
        _skills_tool.HERMES_HOME = profile_dir
        _skills_tool.SKILLS_DIR = profile_dir / "skills"
        _skill_mgr.HERMES_HOME = profile_dir
        _skill_mgr.SKILLS_DIR = profile_dir / "skills"
        try:
            yield profile_dir if token is not None else None
        finally:
            _skills_tool.HERMES_HOME = old_home
            _skills_tool.SKILLS_DIR = old_skills_dir
            _skill_mgr.HERMES_HOME = old_mgr_home
            _skill_mgr.SKILLS_DIR = old_mgr_skills_dir
            if token is not None:
                reset_hermes_home_override(token)


@contextmanager
def _config_profile_scope(profile: Optional[str]):
    """Await-safe, config-only profile scope for handlers that ``await``.

    Unlike ``_profile_scope`` this touches ONLY the context-local
    ``set_hermes_home_override`` contextvar — it does NOT swap the
    process-global ``skills_tool``/``skill_manager`` module attributes.
    Those globals are shared across all event-loop tasks, so holding them
    across an ``await`` lets a concurrent skills request restore THIS
    request's profile dir on its ``finally`` (cross-contamination). The
    contextvar override is task-local and survives an ``await`` cleanly,
    which is all endpoints that resolve ``get_hermes_home()`` at call time
    (config, env, gateway status) actually need.

    None/""/"current" means the dashboard's own profile — no override.
    """
    _reject_authenticated_control_plane_owner_surface("Profile-scoped management APIs")
    requested = (profile or "").strip()
    if not requested or requested.lower() == "current":
        yield None
        return

    from hermes_constants import (
        set_hermes_home_override,
        reset_hermes_home_override,
    )

    profile_dir = _resolve_profile_dir(requested)
    token = set_hermes_home_override(str(profile_dir))
    try:
        yield profile_dir
    finally:
        reset_hermes_home_override(token)


class SkillToggle(BaseModel):
    name: str
    enabled: bool
    profile: Optional[str] = None


@app.get("/api/skills")
async def get_skills(request: Request, profile: Optional[str] = None):
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_owner_http(request)
    from tools.skills_tool import _find_all_skills
    from hermes_cli.skills_config import get_disabled_skills
    with _profile_scope(profile):
        config = load_config()
        disabled = get_disabled_skills(config)
        skills = _find_all_skills(skip_disabled=True)
    for s in skills:
        s["enabled"] = s["name"] not in disabled
    return skills


@app.put("/api/skills/toggle")
async def toggle_skill(request: Request, body: SkillToggle, profile: Optional[str] = None):
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(body.profile)
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_owner_http(request)
    from hermes_cli.skills_config import get_disabled_skills, save_disabled_skills
    with _profile_scope(body.profile or profile):
        config = load_config()
        disabled = get_disabled_skills(config)
        if body.enabled:
            disabled.discard(body.name)
        else:
            disabled.add(body.name)
        save_disabled_skills(config, disabled)
    return {"ok": True, "name": body.name, "enabled": body.enabled}


class SkillCreate(BaseModel):
    name: str
    content: str
    category: Optional[str] = None
    profile: Optional[str] = None


class SkillContentUpdate(BaseModel):
    name: str
    content: str
    profile: Optional[str] = None


class SkillDelete(BaseModel):
    name: str
    profile: Optional[str] = None


def _clear_skills_prompt_cache() -> None:
    """Best-effort: invalidate the skills system-prompt snapshot after a write.

    Mirrors what ``skill_manage`` does so a dashboard-authored skill is picked
    up by the next session without a manual cache reset.
    """
    try:
        from agent.prompt_builder import clear_skills_system_prompt_cache
        clear_skills_system_prompt_cache(clear_snapshot=True)
    except Exception:
        pass


@app.get("/api/skills/content")
async def get_skill_content(request: Request, name: str, profile: Optional[str] = None):
    """Return the raw SKILL.md text for a skill, for the dashboard editor."""
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_owner_http(request)
    from tools.skill_manager_tool import _find_skill

    with _profile_scope(profile):
        found = _find_skill(name)
        if not found:
            raise HTTPException(status_code=404, detail=f"Skill '{name}' not found.")
        skill_md = found["path"] / "SKILL.md"
        if not skill_md.exists():
            raise HTTPException(status_code=404, detail=f"Skill '{name}' has no SKILL.md.")
        try:
            content = skill_md.read_text(encoding="utf-8")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"name": name, "content": content, "path": str(skill_md)}


@app.post("/api/skills")
async def create_skill(request: Request, body: SkillCreate):
    """Create a new custom skill (SKILL.md) from the dashboard editor.

    Calls the same validated write path as the agent's ``skill_manage``
    tool (frontmatter validation, name/category validation, size limit,
    optional security scan) — but bypasses the agent write-approval gate:
    a write from the authenticated dashboard IS the user acting directly.
    """
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(body.profile)
        return await _proxy_authenticated_owner_http(request)
    from tools.skill_manager_tool import _create_skill

    with _profile_scope(body.profile):
        result = _create_skill(body.name, body.content, body.category or None)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to create skill."))
    _clear_skills_prompt_cache()
    return result


@app.delete("/api/skills")
async def delete_skill(request: Request, body: SkillDelete):
    """Permanently delete a skill selected by the authenticated dashboard user."""
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(body.profile)
        return await _proxy_authenticated_owner_http(request)
    from tools.skill_manager_tool import _delete_skill
    from tools.skill_usage import forget

    with _profile_scope(body.profile):
        result = _delete_skill(body.name, absorbed_into="")
        if result.get("success") and not result.get("_archived"):
            forget(body.name)
    if not result.get("success"):
        err = result.get("error", "Failed to delete skill.")
        status = 404 if "not found" in str(err).lower() else 400
        raise HTTPException(status_code=status, detail=err)
    _clear_skills_prompt_cache()
    return result


@app.put("/api/skills/content")
async def update_skill_content(request: Request, body: SkillContentUpdate):
    """Replace the SKILL.md of an existing skill (full rewrite) from the editor."""
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(body.profile)
        return await _proxy_authenticated_owner_http(request)
    from tools.skill_manager_tool import _edit_skill

    with _profile_scope(body.profile):
        result = _edit_skill(body.name, body.content)
    if not result.get("success"):
        err = result.get("error", "Failed to update skill.")
        status = 404 if "not found" in str(err).lower() else 400
        raise HTTPException(status_code=status, detail=err)
    _clear_skills_prompt_cache()
    return result


@app.get("/api/tools/toolsets")
async def get_toolsets(request: Request, profile: Optional[str] = None):
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_owner_http(request)

    from hermes_cli.dashboard_owner_payloads import toolsets_payload

    with _profile_scope(profile):
        return toolsets_payload(load_config())


class ToolsetToggle(BaseModel):
    enabled: bool
    profile: Optional[str] = None


@app.put("/api/tools/toolsets/{name}")
async def toggle_toolset(name: str, body: ToolsetToggle, profile: Optional[str] = None):
    """Enable/disable a configurable toolset for the desktop (cli) platform.

    Persists to ``platform_toolsets.cli`` via the same ``_save_platform_tools``
    helper the CLI ``hermes tools`` picker uses, so the GUI and CLI stay in
    lockstep. Scoped to ``body.profile`` when provided. Returns 400 for
    unknown toolset keys.
    """
    from hermes_cli.tools_config import (
        _get_effective_configurable_toolsets,
        _get_platform_tools,
        _save_platform_tools,
    )

    valid = {ts_key for ts_key, _, _ in _get_effective_configurable_toolsets()}
    if name not in valid:
        raise HTTPException(status_code=400, detail=f"Unknown toolset: {name}")

    with _profile_scope(body.profile or profile):
        config = load_config()
        enabled = set(
            _get_platform_tools(config, "cli", include_default_mcp_servers=False)
        )
        if body.enabled:
            enabled.add(name)
        else:
            enabled.discard(name)
        _save_platform_tools(config, "cli", enabled)
    return {"ok": True, "name": name, "enabled": body.enabled}


@app.get("/api/tools/toolsets/{name}/config")
async def get_toolset_config(name: str, profile: Optional[str] = None):
    """Return the provider matrix + key status for a toolset's config panel.

    Surfaces the same provider rows the CLI ``hermes tools`` picker shows
    (via ``_visible_providers``), each with its ``env_vars`` annotated with
    current ``is_set`` state so the GUI can render provider selection + key
    entry. Toolsets without a ``TOOL_CATEGORIES`` entry return an empty
    provider list and ``has_category: false``. Returns 400 for unknown keys.
    """
    from hermes_cli.tools_config import (
        TOOL_CATEGORIES,
        _get_effective_configurable_toolsets,
        _is_provider_active,
        _visible_providers,
    )
    from hermes_cli.config import get_env_value

    valid = {ts_key for ts_key, _, _ in _get_effective_configurable_toolsets()}
    if name not in valid:
        raise HTTPException(status_code=400, detail=f"Unknown toolset: {name}")

    with _profile_scope(profile):
        config = load_config()
        cat = TOOL_CATEGORIES.get(name)
        providers = []
        active_provider = None
        if cat:
            for prov in _visible_providers(cat, config, force_fresh=True):
                env_vars = [
                    {
                        "key": e["key"],
                        "prompt": e.get("prompt", e["key"]),
                        "url": e.get("url"),
                        "default": e.get("default"),
                        "is_set": bool(get_env_value(e["key"])),
                    }
                    for e in prov.get("env_vars", [])
                ]
                # Surface the same active-provider determination the CLI picker
                # uses (``_is_provider_active``) so the GUI highlights the provider
                # actually written to config (e.g. web.backend), not just the first
                # keyless one in the list.
                is_active = _is_provider_active(prov, config, force_fresh=True)
                if is_active and active_provider is None:
                    active_provider = prov["name"]
                providers.append({
                    "name": prov["name"],
                    "badge": prov.get("badge", ""),
                    "tag": prov.get("tag", ""),
                    "env_vars": env_vars,
                    "post_setup": prov.get("post_setup"),
                    "requires_nous_auth": bool(prov.get("requires_nous_auth")),
                    "is_active": is_active,
                })
    return {
        "name": name,
        "has_category": cat is not None,
        "providers": providers,
        "active_provider": active_provider,
    }


class ToolsetProviderSelect(BaseModel):
    provider: str
    profile: Optional[str] = None


@app.put("/api/tools/toolsets/{name}/provider")
async def select_toolset_provider(
    name: str, body: ToolsetProviderSelect, profile: Optional[str] = None
):
    """Persist a provider selection for a toolset (no key prompting).

    Delegates to ``apply_provider_selection`` — the shared, non-interactive
    core extracted from the CLI configurator — so the GUI and ``hermes tools``
    write identical config keys (``web.backend``, ``tts.provider``, etc.).
    API keys and post-setup flows are handled by separate endpoints. Returns
    400 for unknown toolset or provider names.
    """
    from hermes_cli.tools_config import (
        apply_provider_selection,
        _get_effective_configurable_toolsets,
    )

    valid = {ts_key for ts_key, _, _ in _get_effective_configurable_toolsets()}
    if name not in valid:
        raise HTTPException(status_code=400, detail=f"Unknown toolset: {name}")

    with _profile_scope(body.profile or profile):
        config = load_config()
        try:
            apply_provider_selection(name, body.provider, config)
        except KeyError as exc:
            raise HTTPException(status_code=400, detail=str(exc).strip('"'))
        save_config(config)
    return {"ok": True, "name": name, "provider": body.provider}


class ToolsetEnvUpdate(BaseModel):
    env: Dict[str, str]
    profile: Optional[str] = None


@app.put("/api/tools/toolsets/{name}/env")
async def save_toolset_env(name: str, body: ToolsetEnvUpdate, profile: Optional[str] = None):
    """Persist API keys for a toolset's provider env vars.

    Writes each ``key: value`` to ``~/.hermes/.env`` via ``save_env_value`` —
    the same store ``hermes tools`` writes when it prompts for keys. Keys are
    validated against the env-var allowlist for the toolset's category (the
    union of every visible provider's ``env_vars``), so the GUI can't write an
    arbitrary env var through this endpoint. A blank value is treated as
    "leave unchanged" and skipped. Returns the saved/skipped key lists and the
    refreshed ``is_set`` status. Returns 400 for unknown toolset or env keys.
    """
    from hermes_cli.tools_config import (
        TOOL_CATEGORIES,
        _get_effective_configurable_toolsets,
        _visible_providers,
    )
    from hermes_cli.config import get_env_value, save_env_value

    valid_ts = {ts_key for ts_key, _, _ in _get_effective_configurable_toolsets()}
    if name not in valid_ts:
        raise HTTPException(status_code=400, detail=f"Unknown toolset: {name}")

    with _profile_scope(body.profile or profile):
        config = load_config()
        cat = TOOL_CATEGORIES.get(name)
        allowed: set[str] = set()
        if cat:
            for prov in _visible_providers(cat, config, force_fresh=True):
                for e in prov.get("env_vars", []):
                    allowed.add(e["key"])

        unknown = [k for k in body.env if k not in allowed]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown env var(s) for toolset {name}: {', '.join(sorted(unknown))}",
            )

        saved: List[str] = []
        skipped: List[str] = []
        for key, value in body.env.items():
            if value and value.strip():
                try:
                    save_env_value(key, value.strip())
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc))
                saved.append(key)
            else:
                skipped.append(key)

        status = {k: bool(get_env_value(k)) for k in allowed}
    return {"ok": True, "name": name, "saved": saved, "skipped": skipped, "is_set": status}


class ToolsetPostSetup(BaseModel):
    key: str
    profile: Optional[str] = None


@app.post("/api/tools/toolsets/{name}/post-setup")
async def run_toolset_post_setup(
    name: str, body: ToolsetPostSetup, profile: Optional[str] = None
):
    """Spawn a provider's post-setup install hook as a background action.

    Post-setup hooks (npm install for browser/Camofox, pip install for
    KittenTTS/Piper/ddgs, cua-driver fetch, etc.) are long-running and
    text-output, so this follows the spawn-action pattern: it launches
    ``hermes tools post-setup <key>`` and the frontend tails the log via
    ``GET /api/actions/tools-post-setup/status``. The ``key`` is validated
    against the declared post-setup allowlist before spawning. Returns 400
    for unknown toolset or post-setup key.

    ``profile`` spawns the hook as ``hermes -p <profile> tools post-setup``.
    Most hooks install machine-level artifacts (repo node_modules, shared
    pip packages) where the scope is inert, but hooks that read config or
    write per-profile state must see the same HERMES_HOME the rest of the
    drawer's writes targeted — so the scope is threaded for consistency.
    """
    from hermes_cli.tools_config import (
        _get_effective_configurable_toolsets,
        valid_post_setup_keys,
    )

    valid_ts = {ts_key for ts_key, _, _ in _get_effective_configurable_toolsets()}
    if name not in valid_ts:
        raise HTTPException(status_code=400, detail=f"Unknown toolset: {name}")

    if body.key not in valid_post_setup_keys():
        raise HTTPException(
            status_code=400, detail=f"Unknown post-setup key: {body.key}"
        )

    try:
        proc = _spawn_hermes_action(
            _profile_cli_args(body.profile or profile)
            + ["tools", "post-setup", body.key],
            "tools-post-setup",
        )
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("Failed to spawn tools post-setup")
        raise HTTPException(
            status_code=500, detail=f"Failed to run post-setup: {exc}"
        )
    return {"ok": True, "pid": proc.pid, "name": "tools-post-setup", "key": body.key}


# ---------------------------------------------------------------------------
# Computer Use (cua-driver) — cross-platform readiness + macOS permission grant
#
# cua-driver runs on macOS, Windows, and Linux. The desktop card reflects
# per-OS readiness: on macOS the Accessibility + Screen Recording TCC grants
# (which attach to cua-driver's OWN identity, com.trycua.driver — not Hermes,
# so no app entitlement is involved); elsewhere, driver health from
# `cua-driver doctor`. The grant flow is macOS-only (no TCC toggles to request
# on Windows/Linux).
# ---------------------------------------------------------------------------


@app.get("/api/tools/computer-use/status")
async def get_computer_use_status(profile: Optional[str] = None):
    """Cross-platform Computer Use readiness for the desktop card.

    See ``tools.computer_use.permissions.computer_use_status`` for the payload
    shape. Read-only and fast (shells ``cua-driver doctor`` + macOS
    ``permissions status``).
    """
    from tools.computer_use.permissions import computer_use_status

    with _profile_scope(profile):
        return computer_use_status()


@app.post("/api/tools/computer-use/permissions/grant")
async def grant_computer_use_permissions(profile: Optional[str] = None):
    """Spawn ``hermes computer-use permissions grant`` as a background action.

    macOS-only: ``cua-driver permissions grant`` launches CuaDriver via
    LaunchServices so the TCC dialog is attributed to com.trycua.driver, then
    waits for approval. The frontend polls ``GET /api/actions/computer-use-
    grant/status`` and re-reads ``/status`` once it exits. Windows/Linux have
    no TCC toggles to grant, so this returns 400 there.
    """
    if sys.platform != "darwin":
        raise HTTPException(
            status_code=400,
            detail="Computer Use permission grants are a macOS concept.",
        )
    try:
        proc = _spawn_hermes_action(
            _profile_cli_args(profile)
            + ["computer-use", "permissions", "grant"],
            "computer-use-grant",
        )
    except HTTPException:
        raise
    except Exception as exc:
        _log.exception("Failed to spawn computer-use permissions grant")
        raise HTTPException(
            status_code=500, detail=f"Failed to request permissions: {exc}"
        )
    return {"ok": True, "pid": proc.pid, "name": "computer-use-grant"}


# ---------------------------------------------------------------------------
# Raw YAML config endpoint
# ---------------------------------------------------------------------------


class RawConfigUpdate(BaseModel):
    yaml_text: str
    profile: Optional[str] = None


@app.get("/api/config/raw")
async def get_config_raw(profile: Optional[str] = None):
    """Raw config.yaml text plus its resolved path.

    ``path`` is resolved inside ``_profile_scope`` so the Config page header
    shows the file the switched profile actually reads/writes — /api/status's
    ``config_path`` is machine-global and always reports the dashboard
    process's own profile, which is wrong under the global profile switcher.
    """
    with _profile_scope(profile):
        path = get_config_path()
    if not path.exists():
        return {"yaml": "", "path": str(path)}
    return {"yaml": path.read_text(encoding="utf-8"), "path": str(path)}


@app.put("/api/config/raw")
async def update_config_raw(body: RawConfigUpdate, profile: Optional[str] = None):
    try:
        parsed = yaml.safe_load(body.yaml_text)
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=400, detail="YAML must be a mapping")
        with _profile_scope(body.profile or profile):
            save_config(parsed)
        return {"ok": True}
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")


# ---------------------------------------------------------------------------
# Token / cost analytics endpoint
# ---------------------------------------------------------------------------


def _usage_analytics_from_db(db: Any, days: int = 30) -> dict[str, Any]:
    from hermes_cli.session_analytics import usage_analytics_from_db

    return usage_analytics_from_db(db, days=days)


@app.get("/api/analytics/usage")
async def get_usage_analytics(request: Request, days: int = 30, profile: Optional[str] = None):
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_owner_http(request)

    db = _open_session_db_for_profile(profile)
    try:
        return _usage_analytics_from_db(db, days=days)
    finally:
        db.close()


def _models_analytics_from_db(db: Any, days: int = 30) -> dict[str, Any]:
    from hermes_cli.session_analytics import models_analytics_from_db

    return models_analytics_from_db(db, days=days)


@app.get("/api/analytics/models")
async def get_models_analytics(request: Request, days: int = 30, profile: Optional[str] = None):
    """Rich per-model analytics for the Models dashboard page.

    Returns token/cost/session breakdown per model plus capability metadata
    from models.dev (context window, vision, tools, reasoning, etc.).
    """
    if _authenticated_owner_request(request):
        _reject_authenticated_profile_param(profile)
        return await _proxy_authenticated_owner_http(request)

    db = _open_session_db_for_profile(profile)
    try:
        return _models_analytics_from_db(db, days=days)
    finally:
        db.close()


# WebSocket request and authentication guards shared by dashboard live routes.
_VALID_CHANNEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
# Starlette's TestClient reports the peer as "testclient"; treat it as
# loopback so tests don't need to rewrite request scope.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _ws_client_reason(ws: "WebSocket") -> Optional[str]:
    """Return a rejection reason for the client IP, or None when allowed.

    Reasons are short machine-parseable tokens logged on the rejection path
    so a "WS keeps closing" report can be diagnosed from agent.log without a
    repro. ``None`` means the peer IP passed this gate.

    See :func:`_ws_client_is_allowed` for the full policy rationale.
    """
    state = _ws_app_state(ws)
    if getattr(state, "auth_required", False) or getattr(state, "owner_worker_mode", False):
        return None
    bound_host = (getattr(state, "bound_host", "") or "").strip().lower()
    if bound_host and bound_host not in _LOOPBACK_HOSTS:
        return None
    client_host = ws.client.host if ws.client else ""
    if not client_host:
        # Fail-closed: a loopback-bound dashboard with auth disabled must
        # not accept a WebSocket with no identifiable peer. ASGI servers
        # behind a misconfigured proxy or unix socket can deliver
        # ws.client == None or "" — treating that as "allowed" would let
        # an unidentified peer reach a loopback-only surface.
        return f"missing_or_empty_peer bound={bound_host or '?'}"
    if client_host in _LOOPBACK_HOSTS:
        return None
    return f"peer_not_loopback peer={client_host} bound={bound_host or '?'}"


def _ws_client_is_allowed(ws: "WebSocket") -> bool:
    """Check if the WebSocket client IP is acceptable.

    Loopback bind: only loopback clients are accepted.

    Authenticated mode allows non-loopback peers after Host/Origin and ticket
    validation. Uvicorn's ``proxy_headers=True``
    (enabled when the OAuth gate is active so cookies can pick up
    ``X-Forwarded-Proto``) rewrites ``ws.client.host`` to the
    X-Forwarded-For value, which is the real internet client IP. The
    OAuth gate + single-use ``?ticket=`` is the auth at that point; the
    Host/Origin guard in :func:`_ws_host_origin_is_allowed` is what
    blocks DNS-rebinding here, not the peer IP.
    """
    state = _ws_app_state(ws)
    if getattr(state, "auth_required", False) or getattr(state, "owner_worker_mode", False):
        return True
    # Any explicit non-loopback bind (0.0.0.0, ::, or a specific LAN /
    # Tailscale address) means the operator opted into non-loopback
    # access via --insecure.  The loopback-only peer gate only applies to
    # an actual loopback bind; otherwise the WS handshake is rejected even
    # though same-bind HTTP requests pass _is_accepted_host.
    bound_host = (getattr(state, "bound_host", "") or "").strip().lower()
    if bound_host and bound_host not in _LOOPBACK_HOSTS:
        return True
    client_host = ws.client.host if ws.client else ""
    if not client_host:
        # Fail-closed: see _ws_client_reason for rationale. An empty
        # client_host on a loopback-bound dashboard with auth disabled
        # must be rejected, not accepted as a default-allow.
        return False
    return client_host in _LOOPBACK_HOSTS


def _ws_host_origin_reason(ws: "WebSocket") -> Optional[str]:
    """Return a Host/Origin rejection reason, or None when allowed.

    Mirrors :func:`_ws_host_origin_is_allowed` but yields a short
    machine-parseable token (``host_mismatch …`` / ``origin_mismatch …``)
    on rejection so the close path can log *why* the upgrade was refused.
    """
    state = _ws_app_state(ws)
    bound_host = getattr(state, "bound_host", None)
    if not bound_host:
        return None

    trusted_proxy_host = getattr(state, "trusted_proxy_public_host", "")
    trusted_proxy_origin = getattr(state, "trusted_proxy_public_origin", "")
    host_header = ws.headers.get("host", "")
    if not _is_accepted_host(
        host_header,
        bound_host,
        trusted_proxy_host=trusted_proxy_host,
    ):
        return f"host_mismatch host={host_header or '?'} bound={bound_host}"

    origin = ws.headers.get("origin", "")
    if not origin:
        return None

    parsed = urllib.parse.urlparse(origin)
    if parsed.scheme not in {"http", "https"}:
        # Non-web origin (packaged Electron: file://, null, app://). The
        # upstream credential check is the real auth boundary; trust it.
        # See _ws_host_origin_is_allowed for the full rationale.
        return None

    if not parsed.netloc:
        return f"origin_mismatch origin={origin} bound={bound_host}"

    if trusted_proxy_origin:
        normalized_origin = f"{parsed.scheme}://{parsed.netloc}".lower()
        if normalized_origin != str(trusted_proxy_origin).lower():
            return f"origin_mismatch origin={origin} bound={bound_host}"

    if not _is_accepted_host(
        parsed.netloc,
        bound_host,
        trusted_proxy_host=trusted_proxy_host,
    ):
        return f"origin_mismatch origin={origin} bound={bound_host}"
    return None


def _ws_host_origin_is_allowed(ws: "WebSocket") -> bool:
    """Apply the dashboard Host/Origin guard to WebSocket upgrades.

    FastAPI HTTP middleware does not run for WebSocket routes, so the
    DNS-rebinding Host check used for normal dashboard HTTP requests must be
    repeated here before accepting the upgrade.  Browsers also send an Origin
    header on WebSocket handshakes; when present, require it to target the
    same bound dashboard host.
    """
    return _ws_host_origin_reason(ws) is None


def _ws_request_reason(ws: "WebSocket") -> Optional[str]:
    """First Host/Origin or peer-IP rejection reason, or None when allowed."""
    return _ws_host_origin_reason(ws) or _ws_client_reason(ws)


def _ws_request_is_allowed(ws: "WebSocket") -> bool:
    """Return True when the WebSocket upgrade matches dashboard boundaries."""
    return _ws_host_origin_is_allowed(ws) and _ws_client_is_allowed(ws)


@dataclass(frozen=True)
class _WsAuthResult:
    reason: Optional[str]
    credential: str
    payload: Optional[dict[str, Any]] = None
    # Ephemeral verified-session expiry. It is never signed ticket material and
    # never forwarded to an Owner Worker; bridge admission uses it to terminate
    # a browser connection when its current access session expires.
    session_expires_at: Optional[int] = None


def _ws_app_state(ws: "WebSocket") -> Any:
    return getattr(getattr(ws, "app", None), "state", app.state)


def _ws_auth_mode(ws: "WebSocket | None" = None) -> str:
    """Short label for the active WS auth mode — logged on every connection."""
    state = _ws_app_state(ws) if ws is not None else app.state
    if getattr(state, "owner_worker_mode", False):
        return "owner-worker"
    if getattr(state, "auth_required", False):
        return "gated"
    bound_host = (getattr(state, "bound_host", "") or "").strip().lower()
    if bound_host and bound_host not in _LOOPBACK_HOSTS:
        return "insecure"
    return "loopback"


def _ws_auth_result(ws: "WebSocket") -> _WsAuthResult:
    """Validate WS-upgrade auth and retain the consumed credential payload.

    ``reason`` is None when the credential is accepted, else a short
    machine-parseable token explaining the rejection (``no_credential``,
    ``token_mismatch``, ``ticket_invalid``, ``internal_invalid``).
    ``credential`` names which credential type was presented (``ticket``,
    ``internal``, ``internal_owner_token``, ``token``, or ``none``).

    The public ``_ws_auth_reason`` / ``_ws_auth_ok`` wrappers preserve the old
    tuple / boolean API for existing callers and tests. Authenticated owner
    bridges use this richer result so the Control Plane can derive owner
    context from the server-minted, single-use browser ticket payload without
    consuming the ticket twice.
    """
    state = _ws_app_state(ws)

    if getattr(state, "owner_worker_mode", False):
        # Owner Worker UDS routes must be authorized by a short-lived token
        # bound to this exact owner. The Control Plane's process-lifetime
        # dashboard internal WS credential is intentionally insufficient here.
        owner_key = str(getattr(state, "owner_worker_owner_key", "") or "").strip()
        token = ws.query_params.get("internal_owner_token", "")
        if not token:
            return _WsAuthResult("no_credential", "none")
        try:
            from hermes_cli.dashboard_auth.authority import AuthorityStore
            from hermes_cli.owner_worker.tokens import (
                AUD_OWNER_WORKER_WS,
                SCOPE_OWNER_WORKER_WS,
                OwnerWorkerCapabilityInvalid,
                verify_owner_worker_capability,
            )
            lease = getattr(state, "owner_worker_lease", None)
            verifier = getattr(state, "owner_worker_capability_verifier", {})
            if lease is None:
                raise OwnerWorkerCapabilityInvalid("capability_lease_invalid")
            verify_owner_worker_capability(
                token,
                expected_lease=lease,
                audience=AUD_OWNER_WORKER_WS,
                scope=SCOPE_OWNER_WORKER_WS,
                path=ws.url.path,
                authority_store=AuthorityStore(getattr(state, "owner_worker_control_home", None)),
                public_key=verifier.get("HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY"),
                issuer_key_version=verifier.get("HERMES_OWNER_WORKER_CAPABILITY_ISSUER"),
                retained_public_keys=verifier.get(
                    "HERMES_OWNER_WORKER_CAPABILITY_RETAINED_PUBLIC_KEYS"
                ),
            )
        except Exception:
            return _WsAuthResult("internal_owner_invalid", "internal_owner_token")
        return _WsAuthResult(None, "internal_owner_token", {"owner_key": owner_key})

    auth_required = bool(getattr(state, "auth_required", False))
    if not auth_required:
        return _WsAuthResult("authentication_required", "none")
    if auth_required:
        # Lazy import — keeps this function importable in test harnesses
        # that don't bring in the dashboard_auth layer.
        from hermes_cli.dashboard_auth.audit import (
            AuthorityAuditEvent,
            AuthorityAuditReason,
            audit_authority,
            new_authority_correlation_id,
        )
        from hermes_cli.dashboard_auth.middleware import (
            WebSocketSessionRejected,
            WebSocketSessionUnavailable,
            verify_websocket_ticket_session,
        )
        from hermes_cli.dashboard_auth.ws_tickets import (
            TicketInvalid,
            authority_store,
            browser_ws_audience,
            consume_ticket,
            verify_ticket,
        )

        # Owner-worker-spawned children use a short-lived owner-bound token;
        # route those back through the Control Plane bridge to the matching worker.
        # The old process-lifetime internal credential is intentionally rejected
        # here because it carries no owner context.
        internal_owner_token = ws.query_params.get("internal_owner_token", "")
        if internal_owner_token:
            try:
                from hermes_cli.owner_worker.tokens import AUD_CONTROL_PLANE_WS, validate_internal_token_payload

                supervisor = getattr(state, "owner_worker_supervisor", None)
                payload = validate_internal_token_payload(
                    internal_owner_token,
                    audience=AUD_CONTROL_PLANE_WS,
                    path=ws.url.path,
                    control_home=getattr(supervisor, "control_home", None),
                )
            except Exception:
                payload = None
            if payload:
                return _WsAuthResult(None, "internal_owner_token", payload)
            audit_authority(
                AuthorityAuditEvent.TICKET_REJECTED,
                correlation_id=new_authority_correlation_id(),
                reason=AuthorityAuditReason.INTERNAL_OWNER_INVALID,
            )
            return _WsAuthResult("internal_owner_invalid", "internal_owner_token")

        internal = ws.query_params.get("internal", "")
        if internal:
            audit_authority(
                AuthorityAuditEvent.TICKET_REJECTED,
                correlation_id=new_authority_correlation_id(),
                reason=AuthorityAuditReason.INTERNAL_OWNER_CONTEXT_REQUIRED,
            )
            return _WsAuthResult("internal_owner_context_required", "internal")

        ticket = ws.query_params.get("ticket", "")
        if not ticket:
            return _WsAuthResult("no_credential", "none")
        authority_correlation_id = new_authority_correlation_id()

        try:
            audience = browser_ws_audience(ws.url.path)
            # Validate the ticket first without consuming it, then revalidate
            # its cookie-bound provider session and membership state.  The
            # authority transaction remains last so rejected upgrades cannot
            # burn a valid browser capability.
            ticket_store = authority_store()
            payload = verify_ticket(ticket, audience=audience, store=ticket_store)
            # Reconstruct before worker start/accept so signed-but-inconsistent
            # principal material cannot become an owner routing input.
            from hermes_cli.dashboard_auth.owner_context import owner_context_from_ticket_payload
            owner_context_from_ticket_payload(payload)
            session = verify_websocket_ticket_session(ws, payload)
            payload = consume_ticket(ticket, audience=audience, store=ticket_store)
            audit_authority(
                AuthorityAuditEvent.TICKET_ADMITTED,
                correlation_id=authority_correlation_id,
                reason=AuthorityAuditReason.ADMITTED,
                epoch=int(payload["epoch"]),
            )
            return _WsAuthResult(
                None,
                "ticket",
                payload,
                session_expires_at=int(session.expires_at),
            )
        except WebSocketSessionUnavailable:
            audit_authority(
                AuthorityAuditEvent.AVAILABILITY_FAILURE,
                correlation_id=authority_correlation_id,
                reason=AuthorityAuditReason.SESSION_AUTHORITY_UNAVAILABLE,
            )
            return _WsAuthResult("authority_unavailable", "ticket")
        except TicketInvalid as exc:
            reason = str(exc)
            audit_authority(
                AuthorityAuditEvent.AVAILABILITY_FAILURE
                if reason in {"authority_unavailable", "replay_continuity_unavailable"}
                else AuthorityAuditEvent.TICKET_REJECTED,
                correlation_id=authority_correlation_id,
                reason=(
                    AuthorityAuditReason.AUTHORITY_UNAVAILABLE
                    if reason in {"authority_unavailable", "replay_continuity_unavailable"}
                    else AuthorityAuditReason.TICKET_REJECTED
                ),
            )
            return _WsAuthResult(
                "authority_unavailable" if reason in {"authority_unavailable", "replay_continuity_unavailable"} else "ticket_invalid",
                "ticket",
            )
        except (ValueError, WebSocketSessionRejected):
            audit_authority(
                AuthorityAuditEvent.TICKET_REJECTED,
                correlation_id=authority_correlation_id,
                reason=AuthorityAuditReason.TICKET_REJECTED,
            )
            return _WsAuthResult("ticket_invalid", "ticket")

    return _WsAuthResult("authentication_required", "none")


def _ws_auth_reason(ws: "WebSocket") -> tuple[Optional[str], str]:
    """Validate WS-upgrade auth; return ``(reason, credential)``."""
    result = _ws_auth_result(ws)
    return result.reason, result.credential


def _ws_auth_ok(ws: "WebSocket") -> bool:
    """True when the WS-upgrade credential is accepted. See _ws_auth_reason."""
    return _ws_auth_result(ws).reason is None


def _should_bridge_ws_to_owner_worker(ws: "WebSocket", auth_result: _WsAuthResult) -> bool:
    """Return True when a Control Plane WS carries owner identity to bridge."""
    state = _ws_app_state(ws)
    return bool(
        getattr(state, "auth_required", False)
        and not getattr(state, "owner_worker_mode", False)
        and auth_result.credential in {"ticket", "internal_owner_token"}
    )


def _ws_latency_trace_id(ws: "WebSocket") -> str:
    return clean_latency_trace_id(ws.query_params.get("ws_trace", ""))


def _ws_query_for_owner_worker(ws: "WebSocket", *, internal_owner_bootstrap: str) -> str:
    """Forward browser query params to the worker with external auth stripped."""
    # ``profile=default`` is a harmless management-profile hint in the Control
    # Plane, but inside an owner worker the legacy profile resolver would map it
    # back to the global default profile and overwrite HERMES_HOME. Strip all
    # auth/profile selectors before forwarding; owner identity is already fixed
    # by the worker env and the one-use internal_owner_bootstrap.
    stripped_keys = {"ticket", "token", "internal", "internal_owner_token", "internal_owner_bootstrap", "profile"}
    pairs = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(str(ws.url.query or ""), keep_blank_values=True)
        if key not in stripped_keys
    ]
    pairs.append(("internal_owner_bootstrap", internal_owner_bootstrap))
    return urllib.parse.urlencode(pairs, doseq=True)


def _ws_bridge_close_code(value: Any, default: int = 1011) -> int:
    try:
        code = int(value)
    except (TypeError, ValueError):
        return default
    if 1000 <= code <= 1014 or 3000 <= code <= 4999:
        return code
    return default


_OWNER_WORKER_WS_CONNECT_TIMEOUT = 10.0
_OWNER_WORKER_WS_HANDSHAKE_TIMEOUT = 5.0
_OWNER_WORKER_WS_RELAY_QUEUE_SIZE = 16
_OWNER_WORKER_WS_RELAY_OPERATION_TIMEOUT = 10.0
_OWNER_WORKER_TURN_LEASE_POLL_INTERVAL = 0.1


def _owner_worker_turn_lease_guard_state(app_obj: Any) -> set[asyncio.Task[Any]]:
    try:
        return app_obj.state.owner_worker_turn_lease_guards
    except AttributeError:
        tasks: set[asyncio.Task[Any]] = set()
        app_obj.state.owner_worker_turn_lease_guards = tasks
        return tasks


async def _guard_owner_worker_turn_lease(
    handle: Any,
    supervisor: Any,
    lease: Any,
) -> None:
    """Hold one exact Worker use lease until its admitted turns finish."""
    from hermes_cli.owner_worker.client import OwnerWorkerClient, OwnerWorkerHealthError

    authority_lease = _owner_worker_authority_lease(handle)
    client = OwnerWorkerClient(
        handle.socket_path,
        control_home=getattr(supervisor, "control_home", None),
    )
    try:
        while True:
            health = await asyncio.to_thread(
                client.verify_health,
                owner_key=handle.owner_key,
                owner_home=handle.owner_home,
                worker_generation=handle.worker_generation,
                worker_id=handle.worker_id,
                lease_version=handle.lease_version,
                recovery_generation=handle.recovery_generation,
                lease=authority_lease,
            )
            active_turns = health.get("active_turns")
            if (
                isinstance(active_turns, bool)
                or not isinstance(active_turns, int)
                or active_turns < 0
            ):
                raise OwnerWorkerHealthError(
                    "owner worker active turn status was invalid"
                )
            if active_turns == 0:
                return
            await asyncio.sleep(_OWNER_WORKER_TURN_LEASE_POLL_INTERVAL)
    except Exception:
        # An exited or revoked exact generation no longer needs protection. Health
        # remains fail-closed; only the already-detached use lease is released.
        return
    finally:
        lease.release()


def _schedule_owner_worker_turn_lease_guard(
    app_obj: Any,
    *,
    handle: Any,
    supervisor: Any,
    lease: Any,
) -> None:
    tasks = _owner_worker_turn_lease_guard_state(app_obj)
    try:
        task = asyncio.create_task(
            _guard_owner_worker_turn_lease(handle, supervisor, lease)
        )
    except Exception:
        lease.release()
        raise
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def _drain_owner_worker_turn_lease_guards(app_obj: Any) -> None:
    tasks = tuple(_owner_worker_turn_lease_guard_state(app_obj))
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _report_bridge_lifecycle(lease: Any, reason: "AuthorityAuditReason") -> None:
    """Record trusted bridge lifecycle facts without stream or owner data."""
    try:
        from hermes_cli.dashboard_auth.audit import AuthorityAuditEvent
        from hermes_cli.owner_worker.audit import report_worker_lifecycle

        report_worker_lifecycle(
            AuthorityAuditEvent.BRIDGE_LIFECYCLE,
            reason,
            worker_generation=int(lease.worker_generation),
        )
    except Exception:
        return


class _OwnerWorkerWsRelayClosed(Exception):
    """Stop a relay with a sanitized public close result."""

    def __init__(self, code: int, reason: str = "") -> None:
        self.code = _ws_bridge_close_code(code)
        self.reason = _ws_close_reason(reason) if reason else ""
        super().__init__(self.reason or f"websocket relay closed ({self.code})")


class _OwnerWorkerWsBridge:
    """Own the two relay halves and transfer or release the use lease once."""

    def __init__(self, browser_ws: Any, worker_ws: Any, lease: Any) -> None:
        self.browser_ws = browser_ws
        self.worker_ws = worker_ws
        self.lease = lease
        self._tasks: tuple[asyncio.Task[Any], ...] = ()
        self._closing = False
        self._released = False
        self._lock = asyncio.Lock()

    def set_tasks(self, tasks: tuple[asyncio.Task[Any], ...]) -> None:
        self._tasks = tasks

    @property
    def closing(self) -> bool:
        return self._closing

    async def close(
        self,
        *,
        code: int = 1011,
        reason: str = "",
        release_lease: bool = True,
    ) -> Any | None:
        """Cancel relay work and either release or transfer the use lease."""
        async with self._lock:
            if self._closing:
                return None
            self._closing = True
            current = asyncio.current_task()
            tasks = tuple(task for task in self._tasks if task is not current and not task.done())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            close_code = _ws_bridge_close_code(code)
            close_reason = _ws_close_reason(reason) if reason else ""
            for peer in (self.browser_ws, self.worker_ws):
                try:
                    await peer.close(code=close_code, reason=close_reason)
                except Exception:
                    pass
            transferred_lease = None
            if not self._released and self.lease is not None:
                self._released = True
                if release_lease:
                    self.lease.release()
                else:
                    transferred_lease = self.lease
            if self.lease is not None:
                from hermes_cli.dashboard_auth.audit import AuthorityAuditReason

                _report_bridge_lifecycle(self.lease, AuthorityAuditReason.BRIDGE_CLOSED)
            return transferred_lease


async def _relay_queue_put(queue: asyncio.Queue[Any], value: Any) -> None:
    try:
        await asyncio.wait_for(
            queue.put(value), timeout=_OWNER_WORKER_WS_RELAY_OPERATION_TIMEOUT
        )
    except TimeoutError as exc:
        raise _OwnerWorkerWsRelayClosed(1013, "relay backpressure") from exc


async def _relay_operation(
    operation: Any, *, timeout: float = _OWNER_WORKER_WS_RELAY_OPERATION_TIMEOUT
) -> None:
    try:
        await asyncio.wait_for(operation, timeout=timeout)
    except TimeoutError as exc:
        raise _OwnerWorkerWsRelayClosed(1013, "relay backpressure") from exc
    except WebSocketDisconnect as exc:
        raise _OwnerWorkerWsRelayClosed(
            _ws_bridge_close_code(getattr(exc, "code", None), default=1000),
            str(getattr(exc, "reason", "") or ""),
        ) from exc
    except Exception as exc:
        # The UDS websocket client can expose transport-specific close errors
        # (for example websockets.ConnectionClosedError). Normalize only
        # exceptions that carry a websocket close code; unrelated failures
        # remain visible to the bridge's existing cleanup/diagnostic path.
        close_code = getattr(exc, "code", None)
        close_reason = getattr(exc, "reason", None)
        if close_code is None and close_reason is None:
            raise
        raise _OwnerWorkerWsRelayClosed(
            _ws_bridge_close_code(close_code, default=1011),
            str(close_reason or ""),
        ) from exc


async def _relay_send(peer: Any, value: Any) -> None:
    await _relay_operation(peer.send(value))


async def _connect_owner_worker_ws(socket_path: Path, uri: str, *, open_timeout: float = _OWNER_WORKER_WS_CONNECT_TIMEOUT) -> Any:
    """Open a websocket client through the shared exact-worker transport."""
    from hermes_cli.owner_worker.gateway_client import connect_owner_worker_ws

    return await connect_owner_worker_ws(
        socket_path,
        uri,
        open_timeout=open_timeout,
        max_queue=_OWNER_WORKER_WS_RELAY_QUEUE_SIZE,
    )


def _worker_bridge_identity(lease: Any) -> tuple[str, int, str, int, int]:
    """Return the exact durable Worker fence used for bridge cleanup."""
    return (
        str(lease.owner_key),
        int(lease.worker_generation),
        str(lease.worker_id),
        int(lease.lease_version),
        int(lease.recovery_generation),
    )


def _authorized_ws_bridge_state(
    app_obj: Any,
) -> tuple[dict[str, set[tuple[Any, int]]], asyncio.Lock]:
    """Return Control-Plane browser bridge state for ``app_obj``.

    Each registration retains the admission epoch.  A later membership change
    may keep the same scope digest, so closing by scope alone would incorrectly
    terminate a newly admitted bridge carrying the new epoch.
    """
    try:
        bridges = app_obj.state.authorized_ws_bridges
        lock = app_obj.state.authorized_ws_bridge_lock
    except AttributeError:
        bridges = {}
        lock = asyncio.Lock()
        app_obj.state.authorized_ws_bridges = bridges
        app_obj.state.authorized_ws_bridge_lock = lock
        app_obj.state.authority_change_sequence = 0
        app_obj.state.authority_change_stop = asyncio.Event()
        app_obj.state.authority_change_task = None
    if not hasattr(app_obj.state, "authorized_ws_bridges_by_worker"):
        app_obj.state.authorized_ws_bridges_by_worker = {}
    if not hasattr(app_obj.state, "revoked_ws_bridge_worker_fences"):
        app_obj.state.revoked_ws_bridge_worker_fences = set()
    if not hasattr(app_obj.state, "worker_change_sequence"):
        app_obj.state.worker_change_sequence = 0
    return bridges, lock


async def _close_authorized_bridge_targets(
    app_obj: Any,
    targets: tuple[Any, ...],
    *,
    reason: str,
    code: int = 4401,
) -> None:
    for ws in targets:
        try:
            await ws.close(code=code, reason=_ws_close_reason(f"auth: {reason}"))
        except Exception:
            _log.debug("failed to close revoked browser websocket", exc_info=True)


async def close_authorized_bridges_by_digest(
    app_obj: Any,
    scope_digests: tuple[str, ...],
    *,
    reason: str,
) -> None:
    """Close every local browser bridge for explicitly revoked scopes."""
    bridges, lock = _authorized_ws_bridge_state(app_obj)
    async with lock:
        targets = tuple(
            ws
            for digest in scope_digests
            for ws, _epoch in bridges.pop(str(digest), set())
        )
        if targets:
            worker_bridges = app_obj.state.authorized_ws_bridges_by_worker
            for identity, registered in tuple(worker_bridges.items()):
                kept = registered.difference(targets)
                if kept:
                    worker_bridges[identity] = kept
                else:
                    worker_bridges.pop(identity, None)
    await _close_authorized_bridge_targets(app_obj, targets, reason=reason)


async def close_authorized_bridges_by_changes(
    app_obj: Any,
    changes: tuple[Any, ...],
    *,
    reason: str,
) -> None:
    """Apply shared authority transitions to locally registered bridges."""
    if not changes:
        return
    bridges, lock = _authorized_ws_bridge_state(app_obj)
    async with lock:
        targets: list[Any] = []
        for change in changes:
            digest = str(change.scope_digest)
            registrations = bridges.get(digest, set())
            kept: set[tuple[Any, int]] = set()
            for ws, admitted_epoch in registrations:
                if bool(change.revoked) or admitted_epoch < int(change.epoch):
                    targets.append(ws)
                else:
                    kept.add((ws, admitted_epoch))
            if kept:
                bridges[digest] = kept
            else:
                bridges.pop(digest, None)
        if targets:
            worker_bridges = app_obj.state.authorized_ws_bridges_by_worker
            for identity, registered in tuple(worker_bridges.items()):
                kept = registered.difference(targets)
                if kept:
                    worker_bridges[identity] = kept
                else:
                    worker_bridges.pop(identity, None)
    await _close_authorized_bridge_targets(app_obj, tuple(targets), reason=reason)


async def close_authorized_bridges_by_worker_change(
    app_obj: Any,
    changes: tuple[Any, ...],
    *,
    reason: str,
    close_active: bool = False,
    planned_restart: bool = False,
) -> None:
    """Close only bridges bound to a non-admissible exact Worker fence."""
    if not changes:
        return
    _authorized_ws_bridge_state(app_obj)
    worker_bridges = app_obj.state.authorized_ws_bridges_by_worker
    bridges, lock = _authorized_ws_bridge_state(app_obj)
    targets: set[Any] = set()
    async with lock:
        for change in changes:
            if not close_active and str(change.lease_state) in {"starting", "active"}:
                continue
            identity = _worker_bridge_identity(change)
            app_obj.state.revoked_ws_bridge_worker_fences.add(identity)
            targets.update(worker_bridges.pop(identity, set()))
        if targets:
            for digest, registrations in tuple(bridges.items()):
                kept = {(ws, epoch) for ws, epoch in registrations if ws not in targets}
                if kept:
                    bridges[digest] = kept
                else:
                    bridges.pop(digest, None)
    await _close_authorized_bridge_targets(
        app_obj,
        tuple(targets),
        reason=reason,
        code=1012 if planned_restart else 1011,
    )


async def close_authorized_bridges(app_obj: Any, scope: Any, *, reason: str) -> None:
    """Close local browser bridges for a revoked authority scope."""
    await close_authorized_bridges_by_digest(
        app_obj, (str(scope.digest),), reason=reason
    )


async def _watch_authority_changes(app_obj: Any) -> None:
    """Dispatch shared authority transitions to this replica's bridge registry."""
    from hermes_cli.dashboard_auth.ws_tickets import authority_store

    state = app_obj.state
    while not state.authority_change_stop.is_set():
        try:
            store = authority_store()
            changes, worker_changes = await asyncio.gather(
                asyncio.to_thread(store.changes_since, int(state.authority_change_sequence)),
                asyncio.to_thread(store.worker_changes_since, int(state.worker_change_sequence)),
            )
        except Exception:
            # A replica that cannot read the shared authority cannot safely keep
            # authenticated long-lived connections alive. Admission is already
            # fail-closed; end the local bridges too rather than retrying while
            # they continue with stale authority.
            _log.warning("authority change dispatch unavailable; closing local browser bridges")
            bridges, lock = _authorized_ws_bridge_state(app_obj)
            async with lock:
                targets = tuple(
                    ws for registrations in bridges.values() for ws, _epoch in registrations
                )
                bridges.clear()
                app_obj.state.authorized_ws_bridges_by_worker.clear()
            await _close_authorized_bridge_targets(
                app_obj, targets, reason="authority_unavailable"
            )
            return
        if changes:
            await close_authorized_bridges_by_changes(
                app_obj, changes, reason="authority_transition"
            )
            state.authority_change_sequence = changes[-1].sequence
        if worker_changes:
            await close_authorized_bridges_by_worker_change(
                app_obj, worker_changes, reason="worker_generation_revoked"
            )
            state.worker_change_sequence = worker_changes[-1].sequence
        try:
            await asyncio.wait_for(state.authority_change_stop.wait(), timeout=0.25)
        except asyncio.TimeoutError:
            pass


async def _ensure_authority_change_dispatcher(app_obj: Any) -> None:
    """Start one shared-authority watcher for the local Control Plane."""
    _authorized_ws_bridge_state(app_obj)
    state = app_obj.state
    task = getattr(state, "authority_change_task", None)
    if task is None or task.done():
        state.authority_change_stop = asyncio.Event()
        state.authority_change_task = asyncio.create_task(_watch_authority_changes(app_obj))


def _owner_context_from_ws_auth_result(auth_result: _WsAuthResult) -> Any:
    """Resolve owner routing context from a trusted WS credential payload."""
    from hermes_cli.dashboard_auth.owner_context import (
        owner_context_from_owner_key,
        owner_context_from_ticket_payload,
    )

    if not auth_result.payload:
        raise ValueError("missing owner payload")
    if auth_result.credential == "internal_owner_token":
        supervisor = getattr(app.state, "owner_worker_supervisor", None)
        global_home = getattr(supervisor, "global_home", None)
        return owner_context_from_owner_key(str(auth_result.payload.get("owner_key") or ""), global_home=global_home)
    if auth_result.credential == "ticket":
        return owner_context_from_ticket_payload(auth_result.payload)
    raise ValueError("unsupported owner credential")


async def _bridge_websocket_to_owner_worker(
    ws: "WebSocket",
    *,
    path: str,
    auth_result: _WsAuthResult,
) -> None:
    """Bridge an authenticated browser/internal WS to the owner's worker UDS route."""
    if auth_result.credential not in {"ticket", "internal_owner_token"} or not auth_result.payload:
        await ws.close(code=4401, reason=_ws_close_reason("auth: owner credential required"))
        return

    latency_started_at = time.monotonic()
    latency_trace_id = _ws_latency_trace_id(ws)
    log_latency_stage(
        _log,
        trace_id=latency_trace_id,
        surface="owner-ws-bridge",
        stage="request.received",
    )
    profile = ws.query_params.get("profile", "")
    if profile and profile.strip().lower() not in {"default"}:
        await ws.close(code=4400, reason=_ws_close_reason("profile selection is not available in authenticated mode"))
        return

    from hermes_cli.dashboard_auth.owner_context import ensure_owner_home
    from hermes_cli.owner_worker.tokens import (
        CONNECTION_PURPOSE_INTERACTIVE,
        mint_owner_worker_bootstrap,
        owner_worker_capability_public_config,
        owp1_data,
        parse_owner_worker_bootstrap,
        owp1_hello,
        parse_owp1_data,
        validate_owp1_control,
    )
    try:
        owner = _owner_context_from_ws_auth_result(auth_result)
        ensure_owner_home(owner)
    except Exception as exc:
        _log.warning("owner websocket payload rejected path=%s cred=%s: %s", path, auth_result.credential, _redact_auth_secrets(exc))
        await ws.close(code=4401, reason=_ws_close_reason("auth: owner payload invalid"))
        return

    supervisor = getattr(ws.app.state, "owner_worker_supervisor", None)
    lifecycle = getattr(ws.app.state, "owner_worker_lifecycle", None)
    if supervisor is None:
        await ws.close(code=1013, reason=_ws_close_reason("owner worker supervisor unavailable"))
        return

    lease: Any | None = None
    try:
        with latency_trace_scope(
            _log,
            trace_id=latency_trace_id,
            surface="owner-ws-bridge",
        ):
            handle = await asyncio.to_thread(supervisor.get_or_start, owner)
        lease = _acquire_owner_worker_use(supervisor, handle)
        if lifecycle is not None:
            lifecycle.observe_verified_owner(owner, schedule_start=False)
    except Exception as exc:
        if lease is not None:
            lease.release()
        _log.warning("owner worker start failed for websocket path=%s: %s", path, _redact_auth_secrets(exc))
        await ws.close(code=1013, reason=_ws_close_reason("owner worker unavailable"))
        return

    connection_id = secrets.token_urlsafe(18)
    nonce = secrets.token_urlsafe(18)
    bootstrap_claims = mint_owner_worker_bootstrap(
        _owner_worker_authority_lease(handle),
        path=path,
        connection_id=connection_id,
        nonce=nonce,
        connection_purpose=CONNECTION_PURPOSE_INTERACTIVE,
        control_home=getattr(supervisor, "control_home", None),
    )
    verifier = owner_worker_capability_public_config(getattr(supervisor, "control_home", None))
    bootstrap = parse_owner_worker_bootstrap(
        bootstrap_claims,
        expected_lease=_owner_worker_authority_lease(handle),
        path=path,
        public_key=verifier["HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY"],
        issuer_key_version=verifier["HERMES_OWNER_WORKER_CAPABILITY_ISSUER"],
        retained_public_keys=verifier["HERMES_OWNER_WORKER_CAPABILITY_RETAINED_PUBLIC_KEYS"],
    )
    query = _ws_query_for_owner_worker(ws, internal_owner_bootstrap=bootstrap_claims)
    worker_uri = f"ws://owner-worker{path}"
    if query:
        worker_uri = f"{worker_uri}?{query}"

    worker_ws = None
    try:
        stage_started_at = time.monotonic()
        worker_ws = await _connect_owner_worker_ws(handle.socket_path, worker_uri)
        log_latency_stage(
            _log,
            trace_id=latency_trace_id,
            surface="owner-ws-bridge",
            stage="worker_ws.connected",
            started_at=stage_started_at,
        )
        stage_started_at = time.monotonic()
        await _relay_send(worker_ws, owp1_hello(bootstrap))
        ack = await asyncio.wait_for(
            worker_ws.recv(), timeout=_OWNER_WORKER_WS_HANDSHAKE_TIMEOUT
        )
        validate_owp1_control(ack, bootstrap, message_type="ack")
        log_latency_stage(
            _log,
            trace_id=latency_trace_id,
            surface="owner-ws-bridge",
            stage="owp1.ack",
            started_at=stage_started_at,
        )
    except Exception as exc:
        if worker_ws is not None:
            try:
                await worker_ws.close(code=1011)
            except Exception:
                pass
        if lease is not None:
            if lifecycle is not None:
                lifecycle.report_request_failure(handle, "transport")
            lease.release()
        _log.warning("owner worker websocket connect failed path=%s owner=%s: %s", path, owner.owner_key, _redact_auth_secrets(exc))
        await ws.close(code=1013, reason=_ws_close_reason("owner worker websocket unavailable"))
        return

    await ws.accept()
    log_latency_stage(
        _log,
        trace_id=latency_trace_id,
        surface="owner-ws-bridge",
        stage="browser_ws.accepted",
        started_at=latency_started_at,
    )
    bridge = _OwnerWorkerWsBridge(ws, worker_ws, lease)
    if (
        auth_result.session_expires_at is not None
        and auth_result.session_expires_at <= int(time.time())
    ):
        await bridge.close(code=4401, reason="auth: session_expired")
        return
    bridge_scope_digest: str | None = None
    bridge_worker_identity = _worker_bridge_identity(_owner_worker_authority_lease(handle))
    await _ensure_authority_change_dispatcher(ws.app)
    bridges, bridge_lock = _authorized_ws_bridge_state(ws.app)
    revoked_before_registration = False
    async with bridge_lock:
        if bridge_worker_identity in ws.app.state.revoked_ws_bridge_worker_fences:
            revoked_before_registration = True
        else:
            ws.app.state.authorized_ws_bridges_by_worker.setdefault(
                bridge_worker_identity, set()
            ).add(bridge)
            if auth_result.credential == "ticket":
                from hermes_cli.dashboard_auth.authority import AuthorizationScope

                bridge_scope = AuthorizationScope(
                    provider=str(auth_result.payload["provider"]),
                    tenant_id=str(auth_result.payload["tenant_id"]),
                    user_id=str(auth_result.payload["user_id"]),
                    session_id=str(auth_result.payload["session_id"]),
                    membership_revision=str(auth_result.payload["membership_revision"]),
                )
                bridge_scope_digest = bridge_scope.digest
                bridges.setdefault(bridge_scope_digest, set()).add(
                    (bridge, int(auth_result.payload["epoch"]))
                )
    if revoked_before_registration:
        await bridge.close(code=1011, reason="auth: worker generation revoked")
        return
    from hermes_cli.dashboard_auth.audit import AuthorityAuditReason

    _report_bridge_lifecycle(_owner_worker_authority_lease(handle), AuthorityAuditReason.ADMITTED)
    _log.info("owner websocket bridged path=%s owner=%s", path, owner.owner_key)

    async def expire_browser_session() -> None:
        expires_at = auth_result.session_expires_at
        if expires_at is None:
            return
        delay = max(0, expires_at - int(time.time()))
        await asyncio.sleep(delay)
        await bridge.close(code=4401, reason="auth: session_expired")

    browser_to_worker: asyncio.Queue[Any] = asyncio.Queue(
        maxsize=_OWNER_WORKER_WS_RELAY_QUEUE_SIZE
    )
    worker_to_browser: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(
        maxsize=_OWNER_WORKER_WS_RELAY_QUEUE_SIZE
    )

    async def receive_browser() -> None:
        sequence = 1
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                raise _OwnerWorkerWsRelayClosed(
                    _ws_bridge_close_code(msg.get("code"), default=1000),
                    str(msg.get("reason") or ""),
                )
            if msg.get("bytes") is not None:
                envelope = owp1_data(
                    bootstrap,
                    direction="control-to-worker",
                    sequence=sequence,
                    data=msg["bytes"],
                )
            elif msg.get("text") is not None:
                envelope = owp1_data(
                    bootstrap,
                    direction="control-to-worker",
                    sequence=sequence,
                    text=msg["text"],
                )
            else:
                continue
            await _relay_queue_put(browser_to_worker, envelope)
            sequence += 1

    async def send_worker() -> None:
        while True:
            await _relay_send(worker_ws, await browser_to_worker.get())

    async def receive_worker() -> None:
        sequence = 1
        try:
            async for message in worker_ws:
                kind, payload = parse_owp1_data(
                    message,
                    bootstrap,
                    direction="worker-to-control",
                    expected_sequence=sequence,
                )
                sequence += 1
                await _relay_queue_put(worker_to_browser, (kind, payload))
        except _OwnerWorkerWsRelayClosed:
            raise
        except Exception as exc:
            raise _OwnerWorkerWsRelayClosed(
                _ws_bridge_close_code(getattr(exc, "code", None), default=1011),
                str(getattr(exc, "reason", "") or ""),
            ) from exc
        raise _OwnerWorkerWsRelayClosed(1000)

    async def send_browser() -> None:
        while True:
            kind, payload = await worker_to_browser.get()
            if kind == "bytes":
                await _relay_operation(ws.send_bytes(bytes(payload)))
            else:
                await _relay_operation(ws.send_text(str(payload)))

    expiry_task = (
        asyncio.create_task(expire_browser_session())
        if auth_result.session_expires_at is not None
        else None
    )
    relay_tasks = (
        asyncio.create_task(receive_browser()),
        asyncio.create_task(send_worker()),
        asyncio.create_task(receive_worker()),
        asyncio.create_task(send_browser()),
    )
    bridge.set_tasks((*relay_tasks, *((expiry_task,) if expiry_task is not None else ())))
    # Let an already-expired browser session close the bridge before a relay
    # task can race into its normal disconnect path.
    await asyncio.sleep(0)
    close_code, close_reason = 1011, "owner worker relay ended"
    browser_transport_ended = False
    try:
        wait_targets = (*relay_tasks, *((expiry_task,) if expiry_task is not None else ()))
        done, _pending = await asyncio.wait(wait_targets, return_when=asyncio.FIRST_COMPLETED)
        if expiry_task is not None and expiry_task in done:
            try:
                await expiry_task
            except asyncio.CancelledError:
                # ``bridge.close()`` owns and cancels its registered expiry task
                # during normal relay teardown. Do not turn that expected child
                # cancellation into an ASGI application error, but retain an
                # unexpected timer cancellation for diagnosis.
                if not bridge.closing:
                    raise
            return
        # Let all immediately-completing pumps report their close result before
        # selecting the terminal state. This prevents a cancelled sibling from
        # masking the browser's explicit close frame.
        await asyncio.sleep(0)
        done = {task for task in relay_tasks if task.done()}
        ordered_done = tuple(task for task in relay_tasks if task in done)
        for task in ordered_done:
            if task.cancelled():
                continue
            try:
                task.result()
            except _OwnerWorkerWsRelayClosed as exc:
                close_code, close_reason = exc.code, exc.reason
                browser_transport_ended = task in {relay_tasks[0], relay_tasks[3]}
                break
            except Exception:
                browser_transport_ended = task in {relay_tasks[0], relay_tasks[3]}
                _log.debug("owner websocket relay ended", exc_info=True)
                break
    finally:
        if expiry_task is not None:
            expiry_task.cancel()
            await asyncio.gather(expiry_task, return_exceptions=True)
        if bridge_scope_digest is not None or bridge_worker_identity is not None:
            bridges, bridge_lock = _authorized_ws_bridge_state(ws.app)
            async with bridge_lock:
                if bridge_scope_digest is not None:
                    registered = bridges.get(bridge_scope_digest)
                    if registered is not None:
                        registered = {
                            (registered_bridge, admitted_epoch)
                            for registered_bridge, admitted_epoch in registered
                            if registered_bridge is not bridge
                        }
                        if registered:
                            bridges[bridge_scope_digest] = registered
                        else:
                            bridges.pop(bridge_scope_digest, None)
                worker_registered = ws.app.state.authorized_ws_bridges_by_worker.get(
                    bridge_worker_identity
                )
                if worker_registered is not None:
                    worker_registered.discard(bridge)
                    if not worker_registered:
                        ws.app.state.authorized_ws_bridges_by_worker.pop(
                            bridge_worker_identity, None
                        )
        if not bridge.closing:
            defer_turn_lease = path == "/api/ws" and browser_transport_ended
            guarded_lease = await bridge.close(
                code=close_code,
                reason=close_reason,
                release_lease=not defer_turn_lease,
            )
            if guarded_lease is not None:
                _schedule_owner_worker_turn_lease_guard(
                    ws.app,
                    handle=handle,
                    supervisor=supervisor,
                    lease=guarded_lease,
                )


# Per-channel subscriber registry used by /api/pub and /api/events.
# State is initialized in the application lifespan by _get_event_state.


async def _broadcast_event(app: Any, channel: str, payload: str) -> None:
    """Fan out one publisher frame to every subscriber on `channel`."""
    event_channels, event_lock = _get_event_state(app)
    async with event_lock:
        subs = list(event_channels.get(channel, ()))

    for sub in subs:
        try:
            await sub.send_text(payload)
        except Exception:
            # Subscriber went away mid-send; the /api/events finally clause
            # will remove it from the registry on its next iteration.
            _log.warning("broadcast send failed for subscriber on %s", channel, exc_info=True)


def _channel_or_close_code(ws: WebSocket) -> Optional[str]:
    """Return the channel id from the query string or None if invalid."""
    channel = ws.query_params.get("channel", "")

    return channel if _VALID_CHANNEL_RE.match(channel) else None


_AUTH_SECRET_PARAM_RE = re.compile(r"(?i)(ticket|token|internal|internal_owner_token|authorization)=([^&\s]+)")


def _redact_auth_secrets(text: Any) -> str:
    return _AUTH_SECRET_PARAM_RE.sub(lambda m: f"{m.group(1)}=<redacted>", str(text))


def _ws_close_reason(text: str) -> str:
    """Clamp a WS close reason to the protocol's 123-byte UTF-8 limit.

    RFC 6455 caps the close-frame reason at 123 bytes; uvicorn raises if a
    longer string is passed. Our reasons embed an attacker-controlled origin,
    so truncate defensively rather than crash the close handler.
    """
    text = _redact_auth_secrets(text)
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= 123:
        return text
    return encoded[:120].decode("utf-8", "ignore") + "..."


# ---------------------------------------------------------------------------
# /api/ws — authenticated JSON-RPC bridge to the exact Owner Worker.
#
# The Control Plane never runs an Agent gateway locally. Browser tickets and
# owner-bound internal credentials resolve one owner, one worker generation,
# and one UDS route before any JSON-RPC traffic is accepted.
# ---------------------------------------------------------------------------


@app.websocket("/api/ws")
async def gateway_ws(ws: WebSocket) -> None:
    latency_started_at = time.monotonic()
    latency_trace_id = _ws_latency_trace_id(ws)
    log_latency_stage(
        _log,
        trace_id=latency_trace_id,
        surface="gateway-ws",
        stage="upgrade.received",
    )
    if not _DASHBOARD_EMBEDDED_CHAT_ENABLED:
        await ws.close(code=4403)
        return

    stage_started_at = time.monotonic()
    auth_result = _ws_auth_result(ws)
    log_latency_stage(
        _log,
        trace_id=latency_trace_id,
        surface="gateway-ws",
        stage="auth.checked",
        started_at=stage_started_at,
        outcome="error" if auth_result.reason is not None else "ok",
    )
    if auth_result.reason is not None:
        await ws.close(code=4401, reason=_ws_close_reason(f"auth: {auth_result.reason}"))
        return

    if not _ws_request_is_allowed(ws):
        await ws.close(code=4403)
        return

    if not _should_bridge_ws_to_owner_worker(ws, auth_result):
        await ws.close(
            code=4401,
            reason=_ws_close_reason("auth: owner credential required"),
        )
        return

    log_latency_stage(
        _log,
        trace_id=latency_trace_id,
        surface="gateway-ws",
        stage="owner_bridge.start",
        started_at=latency_started_at,
    )
    await _bridge_websocket_to_owner_worker(ws, path="/api/ws", auth_result=auth_result)


# ---------------------------------------------------------------------------
# /api/pub + /api/events — owner-worker event broadcast routes.
# ---------------------------------------------------------------------------


@app.websocket("/api/pub")
async def pub_ws(ws: WebSocket) -> None:
    if not _DASHBOARD_EMBEDDED_CHAT_ENABLED:
        await ws.close(code=4403)
        return

    auth_result = _ws_auth_result(ws)
    if auth_result.reason is not None:
        await ws.close(code=4401, reason=_ws_close_reason(f"auth: {auth_result.reason}"))
        return

    if not _ws_request_is_allowed(ws):
        await ws.close(code=4403)
        return

    if _should_bridge_ws_to_owner_worker(ws, auth_result):
        await _bridge_websocket_to_owner_worker(ws, path="/api/pub", auth_result=auth_result)
        return

    channel = _channel_or_close_code(ws)
    if not channel:
        await ws.close(code=4400)
        return

    await ws.accept()

    try:
        while True:
            await _broadcast_event(ws.app, channel, await ws.receive_text())
    except WebSocketDisconnect:
        pass


@app.websocket("/api/events")
async def events_ws(ws: WebSocket) -> None:
    if not _DASHBOARD_EMBEDDED_CHAT_ENABLED:
        await ws.close(code=4403)
        return

    auth_result = _ws_auth_result(ws)
    if auth_result.reason is not None:
        await ws.close(code=4401, reason=_ws_close_reason(f"auth: {auth_result.reason}"))
        return

    if not _ws_request_is_allowed(ws):
        await ws.close(code=4403)
        return

    if _should_bridge_ws_to_owner_worker(ws, auth_result):
        await _bridge_websocket_to_owner_worker(ws, path="/api/events", auth_result=auth_result)
        return

    channel = _channel_or_close_code(ws)
    if not channel:
        await ws.close(code=4400)
        return

    await ws.accept()

    event_channels, event_lock = _get_event_state(ws.app)
    async with event_lock:
        event_channels.setdefault(channel, set()).add(ws)

    try:
        while True:
            # Subscribers don't speak — the receive() just blocks until
            # disconnect so the connection stays open as long as the
            # browser holds it.
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        async with event_lock:
            subs = event_channels.get(channel)

            if subs is not None:
                subs.discard(ws)

                if not subs:
                    event_channels.pop(channel, None)


def _normalise_prefix(raw: Optional[str]) -> str:
    """Normalise an X-Forwarded-Prefix header value.

    Thin re-export of :func:`hermes_cli.dashboard_auth.prefix.normalise_prefix`
    — the single source of truth lives in the dashboard_auth package so
    the gate middleware, the OAuth routes, the cookie helpers, and the
    SPA mount all agree on validation rules.
    """
    from hermes_cli.dashboard_auth.prefix import normalise_prefix
    return normalise_prefix(raw)


def mount_spa(application: FastAPI):
    """Mount the built SPA. Falls back to index.html for client-side routing.

    The session token is injected into index.html via a ``<script>`` tag so
    the SPA can authenticate against protected API endpoints without a
    separate (unauthenticated) token-dispensing endpoint.

    When served behind a path-prefix reverse proxy (e.g.
    ``mission-control.tilos.com/hermes/*`` -> local Caddy -> :9119), the
    proxy injects ``X-Forwarded-Prefix: /hermes`` on every request. We
    rewrite the served ``index.html`` so absolute asset URLs (``/assets/...``)
    and the SPA's runtime ``__HERMES_BASE_PATH__`` honour that prefix
    without rebuilding the bundle.
    """
    if not WEB_DIST.exists():
        @application.get("/{full_path:path}")
        async def no_frontend(full_path: str):
            return JSONResponse(
                {"error": "Frontend not built. Run: cd web && npm run build"},
                status_code=404,
            )
        return

    _index_path = WEB_DIST / "index.html"

    def _serve_index(prefix: str = ""):
        """Return index.html with the session token + base-path injected.

        ``prefix`` is the normalised ``X-Forwarded-Prefix`` (e.g. ``/hermes``)
        or empty string when served at root.

        The SPA authenticates through the dashboard session cookie and obtains
        a one-use browser ticket for the exact Owner Worker WebSocket.
        """
        html = _index_path.read_text(encoding="utf-8")
        chat_js = "true" if _DASHBOARD_EMBEDDED_CHAT_ENABLED else "false"
        bootstrap_script = (
            f"<script>"
            f"window.__HERMES_DASHBOARD_EMBEDDED_CHAT__={chat_js};"
            f'window.__HERMES_BASE_PATH__="{prefix}";'
            f"window.__HERMES_AUTH_REQUIRED__=true;"
            f"</script>"
        )
        if prefix:
            # Rewrite absolute asset URLs baked into the Vite build so the
            # browser fetches them through the same proxy prefix.
            html = html.replace('href="/assets/', f'href="{prefix}/assets/')
            html = html.replace('src="/assets/', f'src="{prefix}/assets/')
            html = html.replace('href="/favicon.ico"', f'href="{prefix}/favicon.ico"')
            html = html.replace('href="/fonts/', f'href="{prefix}/fonts/')
            html = html.replace('href="/ds-assets/', f'href="{prefix}/ds-assets/')
            html = html.replace('src="/ds-assets/', f'src="{prefix}/ds-assets/')
        html = html.replace("</head>", f"{bootstrap_script}</head>", 1)
        return HTMLResponse(
            html,
            headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
        )

    # When served behind a path-prefix proxy, the built CSS contains
    # absolute ``url(/fonts/...)`` and ``url(/ds-assets/...)`` references.
    # Browsers resolve those against the document origin, which means
    # under ``/hermes`` they'd hit ``mission-control.tilos.com/fonts/...``
    # (the MC Pages app), not the Hermes backend. Intercept CSS asset
    # requests BEFORE the StaticFiles mount and rewrite the absolute paths
    # when a prefix is in play.
    @application.get("/assets/{filename}.css")
    async def serve_css(filename: str, request: Request):
        css_path = WEB_DIST / "assets" / f"{filename}.css"
        if not css_path.is_file() or not css_path.resolve().is_relative_to(
            WEB_DIST.resolve()
        ):
            return JSONResponse({"error": "not found"}, status_code=404)
        prefix = _normalise_prefix(request.headers.get("x-forwarded-prefix"))
        css = css_path.read_text(encoding="utf-8")
        if prefix:
            for asset_dir in ("/fonts/", "/fonts-terminal/", "/ds-assets/", "/assets/"):
                css = css.replace(f"url({asset_dir}", f"url({prefix}{asset_dir}")
                css = css.replace(f"url(\"{asset_dir}", f"url(\"{prefix}{asset_dir}")
                css = css.replace(f"url('{asset_dir}", f"url('{prefix}{asset_dir}")
        return Response(content=css, media_type="text/css")

    application.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

    @application.get("/{full_path:path}")
    async def serve_spa(full_path: str, request: Request):
        prefix = _normalise_prefix(request.headers.get("x-forwarded-prefix"))
        # An unmatched /api/* path is a missing/renamed endpoint, NOT a
        # client-side route. Falling through to index.html here returns
        # `<!doctype html>` with status 200, which makes JSON clients (the
        # desktop app's fetchJson, dashboard fetch wrappers) blow up with an
        # opaque `SyntaxError: Unexpected token '<'`. Return a real 404 JSON
        # so the caller sees a clear "no such endpoint" instead.
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse(
                {"detail": f"No such API endpoint: /{full_path}"},
                status_code=404,
            )
        file_path = WEB_DIST / full_path
        # Prevent path traversal via url-encoded sequences (%2e%2e/)
        if (
            full_path
            and file_path.resolve().is_relative_to(WEB_DIST.resolve())
            and file_path.exists()
            and file_path.is_file()
        ):
            return FileResponse(file_path)
        return _serve_index(prefix)


# ---------------------------------------------------------------------------
# Dashboard theme endpoints
# ---------------------------------------------------------------------------

# Built-in dashboard themes — label + description only.  The actual color
# definitions live in the frontend (web/src/themes/presets.ts).
_BUILTIN_DASHBOARD_THEMES = [
    {"name": "default",       "label": "Hermes Teal",         "description": "Classic dark teal — the canonical Hermes look"},
    {"name": "default-large", "label": "Hermes Teal (Large)", "description": "Hermes Teal with bigger fonts and roomier spacing"},
    {"name": "nous-blue",     "label": "Nous Blue",           "description": "Light mode — vivid Nous-blue accents on cream canvas"},
    {"name": "midnight",      "label": "Midnight",            "description": "Deep blue-violet with cool accents"},
    {"name": "ember",     "label": "Ember",          "description": "Warm crimson and bronze — forge vibes"},
    {"name": "mono",      "label": "Mono",           "description": "Clean grayscale — minimal and focused"},
    {"name": "cyberpunk", "label": "Cyberpunk",      "description": "Neon green on black — matrix terminal"},
    {"name": "rose",      "label": "Rosé",           "description": "Soft pink and warm ivory — easy on the eyes"},
]


def _parse_theme_layer(value: Any, default_hex: str, default_alpha: float = 1.0) -> Optional[Dict[str, Any]]:
    """Normalise a theme layer spec from YAML into `{hex, alpha}` form.

    Accepts shorthand (a bare hex string) or full dict form.  Returns
    ``None`` on garbage input so the caller can fall back to a built-in
    default rather than blowing up.
    """
    if value is None:
        return {"hex": default_hex, "alpha": default_alpha}
    if isinstance(value, str):
        return {"hex": value, "alpha": default_alpha}
    if isinstance(value, dict):
        hex_val = value.get("hex", default_hex)
        alpha_val = value.get("alpha", default_alpha)
        if not isinstance(hex_val, str):
            return None
        try:
            alpha_f = float(alpha_val)
        except (TypeError, ValueError):
            alpha_f = default_alpha
        return {"hex": hex_val, "alpha": max(0.0, min(1.0, alpha_f))}
    return None


_THEME_DEFAULT_TYPOGRAPHY: Dict[str, str] = {
    "fontSans": 'system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
    "fontMono": 'ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace',
    "baseSize": "15px",
    "lineHeight": "1.55",
    "letterSpacing": "0",
}

_THEME_DEFAULT_LAYOUT: Dict[str, str] = {
    "radius": "0.5rem",
    "density": "comfortable",
}

_THEME_OVERRIDE_KEYS = {
    "card", "cardForeground", "popover", "popoverForeground",
    "primary", "primaryForeground", "secondary", "secondaryForeground",
    "muted", "mutedForeground", "accent", "accentForeground",
    "destructive", "destructiveForeground", "success", "warning",
    "border", "input", "ring",
}

# Well-known named asset slots themes can populate.  Any other keys under
# ``assets.custom`` are exposed as ``--theme-asset-custom-<key>`` CSS vars
# for plugin/shell use.
_THEME_NAMED_ASSET_KEYS = {"bg", "hero", "logo", "crest", "sidebar", "header"}

# Component-style buckets themes can override.  The value under each bucket
# is a mapping from camelCase property name to CSS string; each pair emits
# ``--component-<bucket>-<kebab-property>`` on :root.  The frontend's shell
# components (Card, App header, Backdrop, etc.) consume these vars so themes
# can restyle chrome (clip-path, border-image, segmented progress, etc.)
# without shipping their own CSS.
_THEME_COMPONENT_BUCKETS = {
    "card", "header", "footer", "sidebar", "tab",
    "progress", "badge", "backdrop", "page",
}

_THEME_LAYOUT_VARIANTS = {"standard", "cockpit", "tiled"}

# Cap on customCSS length so a malformed/oversized theme YAML can't blow up
# the response payload or the <style> tag.  32 KiB is plenty for every
# practical reskin (the Strike Freedom demo is ~2 KiB).
_THEME_CUSTOM_CSS_MAX = 32 * 1024


def _normalise_theme_definition(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalise a user theme YAML into the wire format `ThemeProvider`
    expects.  Returns ``None`` if the theme is unusable.

    Accepts both the full schema (palette/typography/layout) and a loose
    form with bare hex strings, so hand-written YAMLs stay friendly.
    """
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return None

    # Palette
    palette_src = data.get("palette", {}) if isinstance(data.get("palette"), dict) else {}
    # Allow top-level `colors.background` as a shorthand too.
    colors_src = data.get("colors", {}) if isinstance(data.get("colors"), dict) else {}

    def _layer(key: str, default_hex: str, default_alpha: float = 1.0) -> Dict[str, Any]:
        spec = palette_src.get(key, colors_src.get(key))
        parsed = _parse_theme_layer(spec, default_hex, default_alpha)
        return parsed if parsed is not None else {"hex": default_hex, "alpha": default_alpha}

    palette = {
        "background": _layer("background", "#041c1c", 1.0),
        "midground": _layer("midground", "#ffe6cb", 1.0),
        "foreground": _layer("foreground", "#ffffff", 0.0),
        "warmGlow": palette_src.get("warmGlow") or data.get("warmGlow") or "rgba(255, 189, 56, 0.35)",
        "noiseOpacity": 1.0,
    }
    raw_noise = palette_src.get("noiseOpacity", data.get("noiseOpacity"))
    try:
        palette["noiseOpacity"] = float(raw_noise) if raw_noise is not None else 1.0
    except (TypeError, ValueError):
        palette["noiseOpacity"] = 1.0

    # Typography
    typo_src = data.get("typography", {}) if isinstance(data.get("typography"), dict) else {}
    typography = dict(_THEME_DEFAULT_TYPOGRAPHY)
    for key in ("fontSans", "fontMono", "fontDisplay", "fontUrl", "baseSize", "lineHeight", "letterSpacing"):
        val = typo_src.get(key)
        if isinstance(val, str) and val.strip():
            typography[key] = val

    # Layout
    layout_src = data.get("layout", {}) if isinstance(data.get("layout"), dict) else {}
    layout = dict(_THEME_DEFAULT_LAYOUT)
    radius = layout_src.get("radius")
    if isinstance(radius, str) and radius.strip():
        layout["radius"] = radius
    density = layout_src.get("density")
    if isinstance(density, str) and density in {"compact", "comfortable", "spacious"}:
        layout["density"] = density

    # Color overrides — keep only valid keys with string values.
    overrides_src = data.get("colorOverrides", {})
    color_overrides: Dict[str, str] = {}
    if isinstance(overrides_src, dict):
        for key, val in overrides_src.items():
            if key in _THEME_OVERRIDE_KEYS and isinstance(val, str) and val.strip():
                color_overrides[key] = val

    # Assets — named slots + arbitrary user-defined keys.  Values must be
    # strings (URLs or CSS ``url(...)``/``linear-gradient(...)`` expressions).
    # We don't fetch remote assets here; the frontend just injects them as
    # CSS vars.  Empty values are dropped so a theme can explicitly clear a
    # slot by setting ``hero: ""``.
    assets_out: Dict[str, Any] = {}
    assets_src = data.get("assets", {}) if isinstance(data.get("assets"), dict) else {}
    for key in _THEME_NAMED_ASSET_KEYS:
        val = assets_src.get(key)
        if isinstance(val, str) and val.strip():
            assets_out[key] = val
    custom_assets_src = assets_src.get("custom")
    if isinstance(custom_assets_src, dict):
        custom_assets: Dict[str, str] = {}
        for key, val in custom_assets_src.items():
            if (
                isinstance(key, str)
                and key.replace("-", "").replace("_", "").isalnum()
                and isinstance(val, str)
                and val.strip()
            ):
                custom_assets[key] = val
        if custom_assets:
            assets_out["custom"] = custom_assets

    # Custom CSS — raw CSS text the frontend injects as a scoped <style>
    # tag on theme apply.  Clipped to _THEME_CUSTOM_CSS_MAX to keep the
    # payload bounded.  We intentionally do NOT parse/sanitise the CSS
    # here — the dashboard is localhost-only and themes are user-authored
    # YAML in ~/.hermes/, same trust level as the config file itself.
    custom_css_val = data.get("customCSS")
    custom_css: Optional[str] = None
    if isinstance(custom_css_val, str) and custom_css_val.strip():
        custom_css = custom_css_val[:_THEME_CUSTOM_CSS_MAX]

    # Component style overrides — per-bucket dicts of camelCase CSS
    # property -> CSS string.  The frontend converts these into CSS vars
    # that shell components (Card, App header, Backdrop) consume.
    component_styles_src = data.get("componentStyles", {})
    component_styles: Dict[str, Dict[str, str]] = {}
    if isinstance(component_styles_src, dict):
        for bucket, props in component_styles_src.items():
            if bucket not in _THEME_COMPONENT_BUCKETS or not isinstance(props, dict):
                continue
            clean: Dict[str, str] = {}
            for prop, value in props.items():
                if (
                    isinstance(prop, str)
                    and prop.replace("-", "").replace("_", "").isalnum()
                    and isinstance(value, (str, int, float))
                    and str(value).strip()
                ):
                    clean[prop] = str(value)
            if clean:
                component_styles[bucket] = clean

    layout_variant_src = data.get("layoutVariant")
    layout_variant = (
        layout_variant_src
        if isinstance(layout_variant_src, str) and layout_variant_src in _THEME_LAYOUT_VARIANTS
        else "standard"
    )

    result: Dict[str, Any] = {
        "name": name,
        "label": data.get("label") or name,
        "description": data.get("description", ""),
        "palette": palette,
        "typography": typography,
        "layout": layout,
        "layoutVariant": layout_variant,
    }
    if color_overrides:
        result["colorOverrides"] = color_overrides
    if assets_out:
        result["assets"] = assets_out
    if custom_css is not None:
        result["customCSS"] = custom_css
    if component_styles:
        result["componentStyles"] = component_styles
    return result


def _discover_user_themes() -> list:
    """Scan ~/.hermes/dashboard-themes/*.yaml for user-created themes.

    Returns a list of fully-normalised theme definitions ready to ship
    to the frontend, so the client can apply them without a secondary
    round-trip or a built-in stub.
    """
    themes_dir = get_hermes_home() / "dashboard-themes"
    if not themes_dir.is_dir():
        return []
    result = []
    for f in sorted(themes_dir.glob("*.yaml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        normalised = _normalise_theme_definition(data)
        if normalised is not None:
            result.append(normalised)
    return result


@app.get("/api/dashboard/themes")
async def get_dashboard_themes():
    """Return available themes and the currently active one.

    Built-in entries ship name/label/description only (the frontend owns
    their full definitions in `web/src/themes/presets.ts`).  User themes
    from `~/.hermes/dashboard-themes/*.yaml` ship with their full
    normalised definition under `definition`, so the client can apply
    them without a stub.
    """
    config = load_config()
    active = cfg_get(config, "dashboard", "theme", default="default")
    user_themes = _discover_user_themes()
    seen = set()
    themes = []
    for t in _BUILTIN_DASHBOARD_THEMES:
        seen.add(t["name"])
        themes.append(t)
    for t in user_themes:
        if t["name"] in seen:
            continue
        themes.append({
            "name": t["name"],
            "label": t["label"],
            "description": t["description"],
            "definition": t,
        })
        seen.add(t["name"])
    return {"themes": themes, "active": active}


class ThemeSetBody(BaseModel):
    name: str


@app.put("/api/dashboard/theme")
async def set_dashboard_theme(body: ThemeSetBody):
    """Set the active dashboard theme (persists to config.yaml)."""
    config = load_config()
    if "dashboard" not in config:
        config["dashboard"] = {}
    config["dashboard"]["theme"] = body.name
    save_config(config)
    return {"ok": True, "theme": body.name}


# Curated font-override ids. Kept in sync with FONT_CHOICES in
# web/src/themes/fonts.ts — the frontend owns the stacks + webfont URLs;
# the backend only needs the id allow-list so it can reject anything not
# in the vetted catalog (the font's webfont URL is injected as a <link>,
# so we never accept an arbitrary user-supplied id/URL here).
from hermes_cli.dashboard_owner_payloads import (
    FONT_CHOICES as _FONT_CHOICES,
    FONT_DEFAULT_ID as _FONT_DEFAULT_ID,
)


@app.get("/api/dashboard/font")
async def get_dashboard_font(request: Request):
    """Return the active font override (``"theme"`` = use the theme's font)."""
    if _authenticated_owner_request(request):
        return await _proxy_authenticated_owner_http(request)
    from hermes_cli.dashboard_owner_payloads import dashboard_font_payload

    return dashboard_font_payload()


class FontSetBody(BaseModel):
    font: str


@app.put("/api/dashboard/font")
async def set_dashboard_font(body: FontSetBody):
    """Set the dashboard font override (persists to config.yaml).

    Accepts any id in the curated catalog, or ``"theme"`` to clear the
    override and fall back to the active theme's own font. Unknown ids are
    coerced to ``"theme"`` rather than 400'd so a stale client can't wedge
    the picker.
    """
    font = body.font if body.font in _FONT_CHOICES else _FONT_DEFAULT_ID
    config = load_config()
    if "dashboard" not in config:
        config["dashboard"] = {}
    config["dashboard"]["font"] = font
    save_config(config)
    return {"ok": True, "font": font}


# ---------------------------------------------------------------------------
# Dashboard plugin system
# ---------------------------------------------------------------------------

# Shared validation/discovery is also used by Owner Workers so manifests have one
# trust model and one api_target interpretation across both runtimes. The
# dashboard_owner_payloads variant (PR #261) honors chat-only plugins and adds
# api_target / authenticated_api passthrough for the legacy #259 API plugins.
from hermes_cli.dashboard_owner_payloads import discover_dashboard_plugins as _discover_dashboard_plugins


# Cache discovered plugins per-process (refresh on explicit re-scan).
_dashboard_plugins_cache: Optional[list] = None


def _get_dashboard_plugins(force_rescan: bool = False) -> list:
    global _dashboard_plugins_cache
    if _dashboard_plugins_cache is None or force_rescan:
        _dashboard_plugins_cache = _discover_dashboard_plugins()
    elif _dashboard_plugins_cache:
        if any(not Path(p["_dir"]).is_dir() for p in _dashboard_plugins_cache):
            _dashboard_plugins_cache = _discover_dashboard_plugins()
    return _dashboard_plugins_cache


@app.get("/api/dashboard/plugins")
async def get_dashboard_plugins(request: Request):
    """Return discovered dashboard plugins (excludes hidden and disabled ones)."""
    if _authenticated_owner_request(request):
        return await _proxy_authenticated_owner_http(request)
    from hermes_cli.dashboard_owner_payloads import active_dashboard_plugin_payload

    return active_dashboard_plugin_payload(_get_dashboard_plugins())


@app.get("/api/dashboard/plugins/rescan")
async def rescan_dashboard_plugins():
    """Force re-scan of dashboard plugins."""
    plugins = _get_dashboard_plugins(force_rescan=True)
    return {"ok": True, "count": len(plugins)}


class _AgentPluginInstallBody(BaseModel):
    identifier: str
    force: bool = False
    enable: bool = True


def _strip_dashboard_manifest(p: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in p.items() if not k.startswith("_")}


def _merged_plugins_hub() -> Dict[str, Any]:
    """Agent discovery + dashboard manifests + optional provider picker metadata."""
    from hermes_cli.plugins_cmd import (
        _discover_all_plugins,
        _get_current_context_engine,
        _get_current_memory_provider,
        _discover_context_engines,
        _discover_memory_providers,
        _get_disabled_set,
        _get_enabled_set,
        _read_manifest as _read_plugin_manifest_at,
    )

    dashboard_list = _get_dashboard_plugins()
    dash_by_name = {str(p["name"]): p for p in dashboard_list}

    disabled_set = _get_disabled_set()
    enabled_set = _get_enabled_set()

    # Read user-hidden plugins from config for the user_hidden field.
    config = load_config()
    hidden_plugins: list = cfg_get(config, "dashboard", "hidden_plugins", default=[]) or []

    plugins_root_resolved = (get_hermes_home() / "plugins").resolve()
    rows: List[Dict[str, Any]] = []

    for name, version, description, source, dir_str, key in _discover_all_plugins():
        # Both the path-derived key (nested category plugins) and the bare
        # manifest name count for enabled/disabled state, matching the runtime
        # loader's back-compat lookup.
        aliases = {name}
        if key:
            aliases.add(key)
        if aliases & disabled_set:
            runtime_status = "disabled"
        elif aliases & enabled_set:
            runtime_status = "enabled"
        else:
            runtime_status = "inactive"

        dir_path = Path(dir_str)
        dm = dash_by_name.get(name)
        has_dash_manifest = dm is not None or (dir_path / "dashboard" / "manifest.json").exists()

        under_user_tree = False
        try:
            dir_path.resolve().relative_to(plugins_root_resolved)
            under_user_tree = True
        except ValueError:
            pass

        can_remove_update = (
            source in {"user", "git"} and under_user_tree and Path(dir_str).is_dir()
        )

        # Check if this plugin provides tools that require auth
        auth_required = False
        auth_command = ""
        manifest_data = _read_plugin_manifest_at(dir_path)
        provides_tools = manifest_data.get("provides_tools") or []
        if provides_tools:
            try:
                from tools.registry import registry
                for tname in provides_tools:
                    entry = registry.get_entry(tname)
                    if entry and entry.check_fn and not entry.check_fn():
                        auth_required = True
                        auth_command = f"hermes auth {name}"
                        break
            except Exception:
                pass

        rows.append({
            "name": name,
            "version": version or "",
            "description": description or "",
            "source": source,
            "runtime_status": runtime_status,
            "has_dashboard_manifest": has_dash_manifest,
            "dashboard_manifest": _strip_dashboard_manifest(dm) if dm else None,
            "path": dir_str,
            "can_remove": can_remove_update,
            "can_update_git": can_remove_update and (Path(dir_str) / ".git").exists(),
            "auth_required": auth_required,
            "auth_command": auth_command,
            "user_hidden": name in hidden_plugins,
        })

    agent_names = {r["name"] for r in rows}
    orphan_dashboard = [
        _strip_dashboard_manifest(p)
        for p in dashboard_list
        if str(p["name"]) not in agent_names
    ]

    memory_providers: List[Dict[str, str]] = []
    try:
        for n, desc in _discover_memory_providers():
            memory_providers.append({"name": n, "description": desc})
    except Exception:
        memory_providers = []

    context_engines: List[Dict[str, str]] = []
    try:
        for n, desc in _discover_context_engines():
            context_engines.append({"name": n, "description": desc})
    except Exception:
        context_engines = []

    return {
        "plugins": rows,
        "orphan_dashboard_plugins": orphan_dashboard,
        "providers": {
            "memory_provider": _get_current_memory_provider() or "",
            "memory_options": memory_providers,
            "context_engine": _get_current_context_engine(),
            "context_options": context_engines,
        },
    }


@app.get("/api/dashboard/plugins/hub")
async def get_plugins_hub(request: Request):
    """Unified agent plugins + dashboard extension metadata (session protected)."""
    try:
        return _merged_plugins_hub()
    except Exception as exc:
        _log.warning("plugins/hub failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to build plugins hub.") from exc


@app.post("/api/dashboard/agent-plugins/install")
async def post_agent_plugin_install(request: Request, body: _AgentPluginInstallBody):
    from hermes_cli.plugins_cmd import dashboard_install_plugin

    result = dashboard_install_plugin(
        body.identifier.strip(),
        force=body.force,
        enable=body.enable,
    )
    if not result.get("ok"):
        raise HTTPException(
            status_code=400,
            detail=result.get("error") or "Install failed.",
        )
    _get_dashboard_plugins(force_rescan=True)
    # Strip internal paths from the response
    result.pop("after_install_path", None)
    return result


def _validate_plugin_name(name: str) -> str:
    """Reject path-traversal attempts in plugin name URL parameters."""
    name = name.strip("/")
    if not name or ".." in name or "\\" in name:
        raise HTTPException(status_code=400, detail="Invalid plugin name.")
    return name


@app.post("/api/dashboard/agent-plugins/{name:path}/enable")
async def post_agent_plugin_enable(request: Request, name: str):
    name = _validate_plugin_name(name)
    from hermes_cli.plugins_cmd import dashboard_set_agent_plugin_enabled

    result = dashboard_set_agent_plugin_enabled(name, enabled=True)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Enable failed.")
    return result


@app.post("/api/dashboard/agent-plugins/{name:path}/disable")
async def post_agent_plugin_disable(request: Request, name: str):
    name = _validate_plugin_name(name)
    from hermes_cli.plugins_cmd import dashboard_set_agent_plugin_enabled

    result = dashboard_set_agent_plugin_enabled(name, enabled=False)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Disable failed.")
    return result


@app.post("/api/dashboard/agent-plugins/{name:path}/update")
async def post_agent_plugin_update(request: Request, name: str):
    name = _validate_plugin_name(name)
    from hermes_cli.plugins_cmd import dashboard_update_user_plugin

    result = dashboard_update_user_plugin(name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Update failed.")
    _get_dashboard_plugins(force_rescan=True)
    return result


@app.delete("/api/dashboard/agent-plugins/{name:path}")
async def delete_agent_plugin(request: Request, name: str):
    name = _validate_plugin_name(name)
    from hermes_cli.plugins_cmd import dashboard_remove_user_plugin

    result = dashboard_remove_user_plugin(name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "Remove failed.")
    _get_dashboard_plugins(force_rescan=True)
    return result


class _PluginProvidersPutBody(BaseModel):
    memory_provider: Optional[str] = None
    context_engine: Optional[str] = None


@app.put("/api/dashboard/plugin-providers")
async def put_plugin_providers(request: Request, body: _PluginProvidersPutBody):
    """Persist memory provider / context engine selection (writes config.yaml)."""
    from hermes_cli.plugins_cmd import (
        _save_context_engine,
        _save_memory_provider,
    )

    if body.memory_provider is not None:
        _save_memory_provider(body.memory_provider)
    if body.context_engine is not None:
        _save_context_engine(body.context_engine)
    return {"ok": True}


class _PluginVisibilityBody(BaseModel):
    hidden: bool


@app.post("/api/dashboard/plugins/{name:path}/visibility")
async def post_plugin_visibility(request: Request, name: str, body: _PluginVisibilityBody):
    """Toggle a plugin's sidebar visibility (persists to config.yaml dashboard.hidden_plugins)."""
    name = _validate_plugin_name(name)

    config = load_config()
    if "dashboard" not in config or not isinstance(config.get("dashboard"), dict):
        config["dashboard"] = {}
    hidden_list: list = config["dashboard"].get("hidden_plugins") or []
    if not isinstance(hidden_list, list):
        hidden_list = []

    if body.hidden and name not in hidden_list:
        hidden_list.append(name)
    elif not body.hidden and name in hidden_list:
        hidden_list.remove(name)

    config["dashboard"]["hidden_plugins"] = hidden_list
    save_config(config)
    return {"ok": True, "name": name, "hidden": body.hidden}


@app.get("/dashboard-plugins/{plugin_name}/{file_path:path}")
async def serve_plugin_asset(plugin_name: str, file_path: str):
    """Serve static assets from a dashboard plugin directory.

    Only serves files from the plugin's ``dashboard/`` subdirectory.
    Path traversal is blocked by checking ``resolve().is_relative_to()``.

    Restricted to a browser-fetchable suffix allowlist (JS/CSS/JSON/HTML/
    SVG/PNG/JPG/WOFF). The dashboard loads plugin JS via ``<script src>``
    and CSS via ``<link href>``, neither of which can attach a custom
    auth header — so this route stays unauthenticated to keep the SPA
    working. But user-installed plugins ship a ``plugin_api.py``
    backend module that the browser never fetches; it's only imported
    by :func:`_mount_plugin_api_routes` at startup. Without a suffix
    allowlist, anyone on the loopback port can curl the ``.py`` source
    of a private third-party plugin. Reject everything outside the
    browser-asset set.

    User plugins must be in plugins.enabled before their assets are
    served. (#46435, GHSA-mcfc-hp25-cjv7)
    """
    plugins = _get_dashboard_plugins()
    plugin = next((p for p in plugins if p["name"] == plugin_name), None)
    if not plugin:
        raise HTTPException(status_code=404, detail="Plugin not found")

    # Gate: user plugins must be enabled to serve assets;
    # bundled plugins must not be explicitly disabled.
    try:
        from hermes_cli.plugins_cmd import _get_enabled_set, _get_disabled_set
        enabled_set = _get_enabled_set()
        disabled_set = _get_disabled_set()
    except Exception:
        enabled_set = set()
        disabled_set = set()
    if plugin.get("source") == "user":
        if plugin_name in disabled_set or plugin_name not in enabled_set:
            raise HTTPException(status_code=404, detail="Plugin not found")
    elif plugin.get("source") == "bundled":
        if plugin_name in disabled_set:
            raise HTTPException(status_code=404, detail="Plugin not found")

    base = Path(plugin["_dir"])
    target = (base / file_path).resolve()

    if not target.is_relative_to(base.resolve()):
        raise HTTPException(status_code=403, detail="Path traversal blocked")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    # Browser-asset suffix allowlist. Everything outside this set is
    # rejected with 404 so we don't leak ``.py`` backend sources, README
    # files, ``.env.example`` templates, etc. — none of which the SPA
    # actually fetches. Add to this set deliberately when a new asset
    # type comes up; do NOT change the default fallback.
    suffix = target.suffix.lower()
    content_types = {
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".html": "text/html",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".ico": "image/x-icon",
        ".woff2": "font/woff2",
        ".woff": "font/woff",
        ".ttf": "font/ttf",
        ".otf": "font/otf",
        ".map": "application/json",
    }
    if suffix not in content_types:
        raise HTTPException(
            status_code=404,
            detail="File not found",
        )
    media_type = content_types[suffix]
    return FileResponse(
        target,
        media_type=media_type,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


# Mount validated trusted plugin APIs before the SPA catch-all. Owner-worker
# targets remain mounted here for local dashboards; authenticated requests are
# classified by exact route and proxied to the owner's worker.
from hermes_cli.dashboard_plugins import mount_dashboard_plugin_apis


def _mount_plugin_api_routes():
    mount_dashboard_plugin_apis(
        app,
        _get_dashboard_plugins(),
        runtime_target="control-plane",
    )


_mount_plugin_api_routes()

# Mount the dashboard auth routes (/login, /auth/*, /api/auth/*) before the
# SPA catch-all so /{full_path:path} doesn't swallow them.  These are
# always mounted — the gate middleware decides whether to enforce auth,
# not whether the routes exist.
from hermes_cli.dashboard_auth.routes import router as _dashboard_auth_router  # noqa: E402
app.include_router(_dashboard_auth_router)

mount_spa(app)


def _maybe_enable_dashboard_thread_traceback_dump() -> bool:
    """Opt in to SIGUSR1 all-thread tracebacks for a running dashboard.

    Tracebacks can include request data, so this is deliberately disabled by
    default.  On supported POSIX hosts, setting HERMES_DASHBOARD_DUMP_THREADS
    lets an operator diagnose a stuck server with ``kill -USR1 <pid>``.
    """
    if not env_var_enabled("HERMES_DASHBOARD_DUMP_THREADS"):
        return False
    try:
        import faulthandler
        import signal

        dump_signal = getattr(signal, "SIGUSR1", None)
        if dump_signal is None:
            return False
        faulthandler.register(dump_signal, file=sys.stderr, all_threads=True, chain=False)
    except (AttributeError, OSError, RuntimeError, ValueError) as exc:
        _log.warning("Dashboard thread traceback dump is unavailable: %s", exc)
        return False
    _log.info("Dashboard thread traceback dump enabled; send SIGUSR1 to dump all threads")
    return True


def _read_bound_port(server: "uvicorn.Server", fallback: int) -> int:
    """Read the OS-assigned port from a live uvicorn server socket.

    After ``server.startup()`` the socket is bound.  Returns the actual
    port so ephemeral (port-0) discovery works without a pre-bind TOCTOU.
    Falls back to *fallback* if the socket list is empty (shouldn't happen
    but guards against uvicorn internals changing).
    """
    if server.servers and server.servers[0].sockets:
        return server.servers[0].sockets[0].getsockname()[1]
    return fallback


def _write_dashboard_ready_file(actual_port: int) -> None:
    """Optionally publish the dashboard port through an atomic ready file.

    Windows Desktop can launch dashboard backends with ``pythonw.exe`` to avoid
    console flashes. That path cannot rely on stdout for the port announcement,
    so Electron passes ``HERMES_DESKTOP_READY_FILE`` and waits for this JSON.
    Normal CLI/dashboard launches still use the stdout READY line below.
    """
    target = os.environ.get("HERMES_DESKTOP_READY_FILE")
    if not target:
        return

    tmp_name = ""
    try:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"port": int(actual_port)}, separators=(",", ":"))
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
            tmp_name = fh.name
        os.replace(tmp_name, path)
    except Exception as exc:
        if tmp_name:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except Exception:
                pass
        _log.warning("Failed to write dashboard ready file %r: %s", target, exc)


def _maybe_open_browser(
    host: str, actual_port: int, open_browser: bool
) -> None:
    """Open the dashboard URL in the user's browser if appropriate.

    Skips on headless Linux (no ``DISPLAY`` / ``WAYLAND_DISPLAY``) to avoid
    TUI browsers (links, lynx) that would SIGHUP the server process.
    Maps ``0.0.0.0`` / ``::`` binds to ``127.0.0.1`` so the browser opens
    a reachable URL.
    """
    if not open_browser:
        return

    import webbrowser

    _has_display = (
        sys.platform != "linux"
        or bool(os.environ.get("DISPLAY"))
        or bool(os.environ.get("WAYLAND_DISPLAY"))
    )
    if not _has_display:
        _log.debug(
            "Skipping browser-open: no DISPLAY or WAYLAND_DISPLAY detected "
            "(headless Linux). Pass --no-open to suppress this detection."
        )
        return

    _display_host = host if host not in ("0.0.0.0", "::") else "127.0.0.1"
    _open_url = f"http://{_display_host}:{actual_port}"
    def _open():
        try:
            time.sleep(1.0)
            webbrowser.open(_open_url)
        except Exception:
            pass

    threading.Thread(target=_open, daemon=True).start()


def start_server(
    host: str = "127.0.0.1",
    port: int = 9119,
    open_browser: bool = True,
    trust_proxy_headers: bool = False,
):
    """Start the authenticated web UI server.

    ``trust_proxy_headers`` is restricted to a loopback bind
    with an operator-declared ``dashboard.public_url``. It trusts forwarded
    metadata only from loopback peers.
    """
    import uvicorn

    _maybe_enable_dashboard_thread_traceback_dump()

    try:
        from hermes_cli.nous_auth_keepalive import start_nous_auth_keepalive

        start_nous_auth_keepalive()
    except Exception as exc:
        _log.debug("Nous auth keepalive did not start: %s", exc)

    # The retained Web surface always uses verified cookie/session auth.
    app.state.auth_required = True
    app.state.trusted_proxy_public_host = ""
    app.state.trusted_proxy_public_origin = ""
    if trust_proxy_headers:
        if host not in _LOOPBACK_HOST_VALUES:
            raise SystemExit(
                "--trust-proxy-headers requires a loopback bind"
            )
        from hermes_cli.dashboard_auth.prefix import resolve_public_url

        public_url = resolve_public_url()
        if not public_url:
            raise SystemExit(
                "--trust-proxy-headers requires a valid dashboard.public_url"
            )
        parsed_public_url = urllib.parse.urlparse(public_url)
        app.state.trusted_proxy_public_host = parsed_public_url.netloc
        app.state.trusted_proxy_public_origin = (
            f"{parsed_public_url.scheme}://{parsed_public_url.netloc}"
        )
    app.state.owner_worker_supervisor = None
    app.state.session_reader_supervisor = None
    app.state.authority_store = None
    app.state.authority_lifecycle_lock = None
    if app.state.auth_required:
        from hermes_cli.deployment_media import (
            DeploymentMediaPolicyInvalid,
            policy_from_control_plane_environment as media_policy_from_environment,
        )
        from hermes_cli.deployment_inference import (
            DeploymentInferencePolicyInvalid,
            load_deployment_inference_policy,
            policy_from_control_plane_environment,
        )
        from hermes_cli.dashboard_auth.authority import AuthorityStore
        from hermes_cli.owner_worker import OwnerWorkerSupervisor
        from hermes_cli.owner_worker.cgroup_v2 import CgroupV2Manager
        from hermes_cli.owner_worker.tool_executor_sandbox import load_sandbox_deployment_policy
        from hermes_cli.session_reader import SessionReaderSupervisor

        policy_spec = os.environ.get("HERMES_DEPLOYMENT_INFERENCE_POLICY", "")
        try:
            deployment_inference_policy = load_deployment_inference_policy(policy_spec)
            deployment_inference_policy_resolver = (
                policy_from_control_plane_environment
                if policy_spec.strip()
                == "hermes_cli.deployment_inference:policy_from_control_plane_environment"
                else None
            )
        except DeploymentInferencePolicyInvalid as exc:
            raise RuntimeError("deployment inference policy is invalid") from exc
        try:
            deployment_media_policy = media_policy_from_environment()
        except DeploymentMediaPolicyInvalid as exc:
            raise RuntimeError("deployment media policy is invalid") from exc
        sandbox_policy_spec = os.environ.get("HERMES_SANDBOX_DEPLOYMENT_POLICY", "")
        try:
            sandbox_deployment_policy = load_sandbox_deployment_policy(sandbox_policy_spec)
            if sandbox_deployment_policy.resource_policy is None:
                raise RuntimeError("sandbox deployment resource policy is unavailable")
            resource_manager = CgroupV2Manager(
                sandbox_deployment_policy.resource_policy,
                recover_stale_scopes=(
                    sandbox_deployment_policy.recover_stale_resource_scopes
                ),
            )
        except Exception:
            # Authenticated chat remains available, but no resource descriptor is
            # inherited and the Owner Worker therefore refuses all tool admission.
            resource_manager = None
            _log.warning("authenticated tool resource governance is unavailable")

        worker_scheme = "wss" if os.environ.get("HERMES_DASHBOARD_EXTERNAL_SCHEME", "").lower() == "https" else "ws"
        worker_host = os.environ.get("HERMES_DASHBOARD_EXTERNAL_HOST", "").strip() or host
        worker_netloc = f"[{worker_host}]:{port}" if ":" in worker_host and not worker_host.startswith("[") else f"{worker_host}:{port}"
        global_home = get_hermes_home()

        def revoke_generation_bridges(
            lease: Any,
            *,
            planned_restart: bool = False,
        ) -> None:
            loop = getattr(app.state, "server_event_loop", None)
            if loop is None or loop.is_closed():
                return
            future = asyncio.run_coroutine_threadsafe(
                close_authorized_bridges_by_worker_change(
                    app,
                    (lease,),
                    reason=(
                        "service_restart"
                        if planned_restart
                        else "worker_generation_revoked"
                    ),
                    close_active=True,
                    planned_restart=planned_restart,
                ),
                loop,
            )
            future.result()

        control_home = global_home / "control-plane"
        from hermes_cli.dashboard_auth.lifecycle import acquire_authority_server_lock

        app.state.authority_lifecycle_lock = acquire_authority_server_lock(control_home)
        authority_store = AuthorityStore(control_home)
        try:
            authority_store.ensure_ready()
            app.state.authority_store = authority_store
            from hermes_cli.dashboard_auth import get_provider, register_provider
            from hermes_cli.dashboard_auth.machine_credentials import MachineCredentialProvider

            if get_provider(MachineCredentialProvider.name) is None:
                register_provider(MachineCredentialProvider(authority_store))
            app.state.session_reader_supervisor = SessionReaderSupervisor(
                global_home=global_home,
                control_home=control_home,
                resource_manager=resource_manager,
                authority_store=authority_store,
            )
            app.state.owner_worker_supervisor = OwnerWorkerSupervisor(
                global_home=global_home,
                control_home=control_home,
                authority_store=authority_store,
                control_ws_base=f"{worker_scheme}://{worker_netloc}",
                generation_bridge_revoker=revoke_generation_bridges,
                deployment_inference_policy=deployment_inference_policy,
                deployment_inference_policy_resolver=deployment_inference_policy_resolver,
                deployment_media_policy=deployment_media_policy,
                resource_manager=resource_manager,
            )
        except Exception:
            session_reader_supervisor = getattr(
                app.state, "session_reader_supervisor", None
            )
            if session_reader_supervisor is not None:
                session_reader_supervisor.shutdown()
                app.state.session_reader_supervisor = None
            if resource_manager is not None:
                resource_manager.close()
            app.state.authority_store = None
            app.state.authority_lifecycle_lock.close()
            app.state.authority_lifecycle_lock = None
            raise

    if app.state.auth_required:
        # The gate engages on every non-loopback bind. Require at least one
        # provider to be registered, else fail closed — there is no longer an
        # escape hatch that serves the dashboard without authentication.
        from hermes_cli.dashboard_auth import list_providers
        if not list_providers():
            # Surface the *specific* reason any bundled provider declined
            # to register (e.g. missing HERMES_DASHBOARD_OAUTH_CLIENT_ID).
            # Each provider plugin that ships with Hermes Agent exposes a
            # module-level ``LAST_SKIP_REASON`` string for this purpose;
            # without it the operator would only see "no providers" which
            # is misleading when the provider IS installed but unconfigured.
            skip_reasons: list[str] = []
            try:
                from plugins.dashboard_auth import nous as _nous_plugin

                if _nous_plugin.LAST_SKIP_REASON:
                    skip_reasons.append(
                        f"  • nous: {_nous_plugin.LAST_SKIP_REASON}"
                    )
            except Exception:
                pass

            _fix_hint = (
                "Configure an auth provider before exposing the dashboard:\n"
                "  • Password: set dashboard.basic_auth.username + "
                "password_hash in config.yaml\n"
                "    (hash with: python -c \"from "
                "plugins.dashboard_auth.basic import hash_password; "
                "print(hash_password('your-password'))\")\n"
                "  • OAuth: run `hermes dashboard register` (Nous Portal) or "
                "install a DashboardAuthProvider plugin.\n"
                "There is no unauthenticated public-bind option — to keep it "
                "local, bind 127.0.0.1 and tunnel in (SSH / Tailscale)."
            )
            if skip_reasons:
                raise SystemExit(
                    f"Refusing to bind dashboard to {host} — the auth gate "
                    f"engages on non-loopback binds, but no auth providers "
                    f"are registered.\n\n"
                    f"Bundled providers reported these issues:\n"
                    + "\n".join(skip_reasons)
                    + "\n\n"
                    + _fix_hint
                )
            raise SystemExit(
                f"Refusing to start dashboard on {host} — authentication is "
                f"required, but no auth providers are registered.\n\n" + _fix_hint
            )
        _log.info(
            "Dashboard binding to %s with auth gate enabled. Providers: %s",
            host,
            ", ".join(p.name for p in list_providers()),
        )

    # Record the bound host so host_header_middleware can validate incoming
    # Host headers against it. Defends against DNS rebinding (GHSA-ppp5-vxwm-4cf7).
    app.state.bound_host = host

    # ── Start uvicorn with direct Server API ─────────────────────────
    # We use uvicorn.Server directly (not uvicorn.run) so we can split
    # startup from the main loop.  After startup() the socket is actually
    # bound — we read the OS-assigned port from the live socket, print
    # HERMES_DASHBOARD_READY, open the browser, *then* serve.
    #
    # This eliminates the TOCTOU of the old pre-bind-then-close approach
    # (bind port 0 → close → uvicorn rebind): the socket is held by
    # uvicorn the entire time, so no other process can steal the port.
    #
    # For explicit non-zero ports, if the port is taken uvicorn catches
    # OSError inside create_server() and exits with a clear error — no
    # separate preflight probe needed.
    # Loopback binds are the Desktop case: a single local client, no reverse
    # proxy in front. uvicorn's ws keepalive ping runs ON the same event loop
    # as agent turns, and a single synchronous GIL-holding call on a worker
    # thread (e.g. a regex/scrub over a large model output, or a long
    # delegate_task subagent turn) can starve that loop for *minutes* — the
    # loop cannot process the incoming pong, so uvicorn declares the socket
    # dead and closes it, dropping an otherwise-healthy local connection
    # (#53773: "event loop stalled 226.3s"; #48445/#50005). A longer timeout
    # only raises the threshold — a multi-minute stall sails past any finite
    # window. The keepalive ping exists to detect *half-open* connections
    # (reverse-proxy 524, dropped tunnels), which cannot happen on loopback:
    # there is no network or proxy in the path, and a dead local client tears
    # the socket down with a real FIN/RST that starlette surfaces as
    # WebSocketDisconnect regardless of the ping. So on loopback the ping
    # provides ~no liveness value while actively killing recoverable stalls —
    # disable it entirely. Non-loopback binds sit behind a Cloudflare Tunnel
    # (idle timeout ~100s) where half-open IS a real failure mode, so keep the
    # ping at 20/20 to detect it promptly and stay under the tunnel's idle
    # window.
    _is_loopback = host in ("127.0.0.1", "localhost", "::1")
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning",
        # A forced loopback gate normally serves an SSH tunnel and must not
        # trust forwarded metadata by default. An explicit trusted-proxy mode
        # accepts it only from loopback peers and requires a declared public URL.
        proxy_headers=trust_proxy_headers,
        **(
            {"forwarded_allow_ips": "127.0.0.1,::1"}
            if trust_proxy_headers
            else {}
        ),
        # Half-open detection for public binds only (see above). Loopback
        # disables the protocol ping (None) so an event-loop stall can never
        # trigger a false disconnect; a genuinely dead local client is still
        # reaped via the WebSocketDisconnect → disconnect/reap path.
        ws_ping_interval=None if _is_loopback else 20.0,
        ws_ping_timeout=None if _is_loopback else 20.0,
    )
    server = uvicorn.Server(config)

    async def _serve():
        # Split startup from main_loop so we can read the bound port
        # after the socket is live (ephemeral port discovery).
        if not config.loaded:
            config.load()
        server.lifespan = config.lifespan_class(config)
        with server.capture_signals():
            await server.startup()
            if server.should_exit:
                return

            actual_port = _read_bound_port(server, fallback=port)
            app.state.bound_port = actual_port
            supervisor = getattr(app.state, "owner_worker_supervisor", None)
            if supervisor is not None:
                worker_scheme = "wss" if os.environ.get("HERMES_DASHBOARD_EXTERNAL_SCHEME", "").lower() == "https" else "ws"
                worker_host = os.environ.get("HERMES_DASHBOARD_EXTERNAL_HOST", "").strip() or host
                worker_netloc = f"[{worker_host}]:{actual_port}" if ":" in worker_host and not worker_host.startswith("[") else f"{worker_host}:{actual_port}"
                supervisor.control_ws_base = f"{worker_scheme}://{worker_netloc}"

            _write_dashboard_ready_file(actual_port)
            print(f"HERMES_DASHBOARD_READY port={actual_port}", flush=True)
            print(f"  Hermes Web UI → http://{host}:{actual_port}")
            _maybe_open_browser(host, actual_port, open_browser)

            # Collapse the peer-hangup teardown flood (#50005). When the Desktop
            # forcibly closes its WebSocket mid-write, asyncio logs a full
            # traceback per pending connection-lost callback — 50+ identical
            # WinError 10054 (ConnectionResetError) lines per disconnect on
            # Windows. This filter downgrades exactly that class to one debug
            # line and passes every other loop error through unchanged.
            try:
                from tui_gateway.loop_noise import install_loop_noise_filter

                install_loop_noise_filter(asyncio.get_running_loop())
            except Exception as exc:  # pragma: no cover - best-effort
                _log.debug("loop noise filter install skipped: %s", exc)

            # ── Loop heartbeat watchdog (CF-1) ───────────────────────────
            # Confirm the GIL-pressure hypothesis in production. Re-arm a 2s
            # tick and measure the drift between when it *should* fire and
            # when it actually does: a healthy loop drifts ~0, but a turn that
            # holds the GIL blocks the loop and the next tick fires late by the
            # stall duration. We log that so a stalled-loop WS drop is
            # diagnosable from the gateway log. Uses loop.time() (monotonic)
            # for drift, and call_later (not a task) so it dies with the loop —
            # nothing to cancel on shutdown.
            _hb_interval = 2.0
            _hb_stall_threshold = 5.0
            _hb_loop = asyncio.get_running_loop()

            def _loop_heartbeat(expected: float) -> None:
                now = _hb_loop.time()
                drift = now - expected
                if drift > _hb_stall_threshold:
                    _log.warning(
                        "event loop stalled %.1fs (GIL pressure suspected)",
                        drift,
                    )
                _hb_loop.call_later(
                    _hb_interval, _loop_heartbeat, now + _hb_interval
                )

            _hb_loop.call_later(
                _hb_interval, _loop_heartbeat, _hb_loop.time() + _hb_interval
            )

            await server.main_loop()
            if server.started:
                await server.shutdown()

    def close_authority_lifecycle_lock() -> None:
        lifecycle_lock = getattr(app.state, "authority_lifecycle_lock", None)
        if lifecycle_lock is not None:
            lifecycle_lock.close()
            app.state.authority_lifecycle_lock = None

    # On POSIX, keep the long-standing ``asyncio.run(_serve())`` behavior
    # unchanged — Python's default loop there is already a SelectorEventLoop
    # (or uvloop when uvicorn[standard] installs it), which is exactly what
    # uvicorn serves on. Touching that path would only widen the blast radius
    # for no benefit.
    #
    # On Windows it is broken: ``asyncio.run`` defaults to a ProactorEventLoop,
    # but uvicorn's socket-serving stack assumes a SelectorEventLoop on win32
    # (``uvicorn/loops/asyncio.py`` forces it, and ``uvicorn.Server.run`` threads
    # ``config.get_loop_factory()`` into its runner for exactly this reason).
    # Driving uvicorn on the proactor loop makes ``server.startup()`` bind a
    # socket that never accepts — the dashboard / desktop backend prints
    # "Skipping web UI build" and then hangs forever with the port LISTENING but
    # no TCP handshake completing (#50641). So *only on Windows* we mirror
    # uvicorn's own machinery and run on the loop factory it picks.
    if sys.platform != "win32":
        try:
            asyncio.run(_serve())
        finally:
            close_authority_lifecycle_lock()
        return

    # Windows-only path. Resolve the runner + loop factory FIRST (and fall back
    # to a hand-installed Windows selector policy only when uvicorn predates the
    # loop-factory API, < 0.36). The actual serve call is then OUTSIDE the
    # try/except so genuine serve-time errors (port in use, KeyboardInterrupt)
    # propagate normally instead of being swallowed and double-run.
    try:
        from uvicorn._compat import asyncio_run as _runner

        _loop_factory = config.get_loop_factory()
    except Exception:
        _runner = None
        _loop_factory = None
        try:
            asyncio.set_event_loop_policy(
                asyncio.WindowsSelectorEventLoopPolicy()  # type: ignore[attr-defined]
            )
        except Exception:
            pass

    try:
        if _runner is not None:
            _runner(_serve(), loop_factory=_loop_factory)
        else:
            asyncio.run(_serve())
    finally:
        close_authority_lifecycle_lock()
