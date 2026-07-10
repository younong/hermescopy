"""Owner-worker-local WebSocket routes.

These handlers intentionally avoid importing ``hermes_cli.web_server`` so an
Owner Worker does not construct or accidentally depend on the Control Plane's
module-global FastAPI app/state.  The Control Plane authenticates external
browser WebSockets, then bridges them to these worker-local UDS routes with an
owner-bound ``internal_owner_token``.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib import parse as urllib_parse

from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from hermes_cli.config import get_hermes_home
from hermes_cli.owner_runtime import resolve_workspace_cwd
from hermes_cli.owner_worker.tokens import (
    AUD_CONTROL_PLANE_WS,
    AUD_OWNER_WORKER_WS,
    child_token_ttl_seconds,
    mint_internal_token,
    validate_internal_token,
)

try:
    if sys.platform.startswith("win"):
        from hermes_cli.win_pty_bridge import WinPtyBridge as PtyBridge, PtyUnavailableError
    else:
        from hermes_cli.pty_bridge import PtyBridge, PtyUnavailableError
    _PTY_BRIDGE_AVAILABLE = True
except ImportError:  # pragma: no cover - optional platform dependency missing
    PtyBridge = None  # type: ignore[assignment]
    _PTY_BRIDGE_AVAILABLE = False

    class PtyUnavailableError(RuntimeError):  # type: ignore[no-redef]
        """Stub when the platform PTY bridge cannot be imported."""


_RESIZE_RE = re.compile(rb"\x1b\[RESIZE:(\d+);(\d+)\]")
_PTY_READ_CHUNK_TIMEOUT = 0.2
_VALID_CHANNEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_VALID_BROWSER_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


def _owner_key(app: Any) -> str:
    return str(getattr(app.state, "owner_worker_owner_key", "") or "").strip()


def _control_home(app: Any) -> str | Path | None:
    return getattr(app.state, "owner_worker_control_home", None)


def _ws_close_reason(text: str) -> str:
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= 123:
        return text
    return encoded[:120].decode("utf-8", "ignore") + "..."


def _owner_token_valid(ws: WebSocket) -> bool:
    owner_key = _owner_key(ws.app)
    token = ws.query_params.get("internal_owner_token", "")
    return bool(
        owner_key
        and token
        and validate_internal_token(
            token,
            owner_key,
            audience=AUD_OWNER_WORKER_WS,
            path=ws.url.path,
            control_home=_control_home(ws.app),
        )
    )


async def _require_owner_token_or_close(ws: WebSocket) -> bool:
    if _owner_token_valid(ws):
        return True
    await ws.close(code=4401, reason=_ws_close_reason("auth: internal_owner_invalid"))
    return False


def _get_event_state(app: Any) -> tuple[dict[str, set[WebSocket]], asyncio.Lock]:
    try:
        return app.state.event_channels, app.state.event_lock
    except AttributeError:
        app.state.event_channels = {}
        app.state.event_lock = asyncio.Lock()
        return app.state.event_channels, app.state.event_lock


def _get_chat_argv_lock(app: Any) -> asyncio.Lock:
    try:
        return app.state.chat_argv_lock
    except AttributeError:
        app.state.chat_argv_lock = asyncio.Lock()
        return app.state.chat_argv_lock


def _get_pty_browser_state(app: Any) -> tuple[dict[str, dict[str, Any]], asyncio.Lock]:
    try:
        return app.state.pty_browser_sessions, app.state.pty_browser_lock
    except AttributeError:
        app.state.pty_browser_sessions = {}
        app.state.pty_browser_lock = asyncio.Lock()
        return app.state.pty_browser_sessions, app.state.pty_browser_lock


def _get_pty_active_session_files(app: Any) -> dict[str, Path]:
    try:
        return app.state.pty_active_session_files
    except AttributeError:
        app.state.pty_active_session_files = {}
        return app.state.pty_active_session_files


def _browser_id_or_none(ws: WebSocket) -> Optional[str]:
    browser_id = ws.query_params.get("browser_id", "")
    return browser_id if _VALID_BROWSER_ID_RE.match(browser_id) else None


def _channel_or_none(ws: WebSocket) -> Optional[str]:
    channel = ws.query_params.get("channel", "")
    return channel if _VALID_CHANNEL_RE.match(channel) else None


def _active_session_file_for_channel(app: Any, channel: str) -> Path:
    files = _get_pty_active_session_files(app)
    existing = files.get(channel)
    if existing is not None:
        return existing

    base = get_hermes_home() / "runtime" / "pty-active-sessions"
    base.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(prefix="hermes-pty-active-", suffix=".json", dir=str(base))
    os.close(fd)
    path = Path(raw_path)
    files[channel] = path
    return path


def _read_active_session_file(path: Path) -> Optional[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    session_id = str(data.get("session_id") or "").strip()
    return session_id or None


def _forget_active_session_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


async def _register_browser_pty_owner(app: Any, *, browser_id: str, channel: str, owner_id: str, ws: WebSocket) -> Optional[dict[str, Any]]:
    browser_sessions, browser_lock = _get_pty_browser_state(app)
    async with browser_lock:
        previous = browser_sessions.get(browser_id)
        browser_sessions[browser_id] = {
            "channel": channel,
            "started_at": time.time(),
            "owner_id": owner_id,
            "ws": ws,
            "bridge": None,
        }
        return previous


async def _browser_pty_owner_is_current(app: Any, *, browser_id: str, owner_id: str) -> bool:
    browser_sessions, browser_lock = _get_pty_browser_state(app)
    async with browser_lock:
        existing = browser_sessions.get(browser_id)
        return existing is not None and existing.get("owner_id") == owner_id


async def _attach_browser_pty_bridge(app: Any, *, browser_id: str, owner_id: str, bridge: Any) -> bool:
    browser_sessions, browser_lock = _get_pty_browser_state(app)
    async with browser_lock:
        existing = browser_sessions.get(browser_id)
        if existing is None or existing.get("owner_id") != owner_id:
            return False
        existing["bridge"] = bridge
        return True


async def _release_browser_pty_owner(app: Any, *, browser_id: str, owner_id: str) -> None:
    browser_sessions, browser_lock = _get_pty_browser_state(app)
    async with browser_lock:
        existing = browser_sessions.get(browser_id)
        if existing is not None and existing.get("owner_id") == owner_id:
            browser_sessions.pop(browser_id, None)


async def _close_replaced_browser_pty(owner: dict[str, Any]) -> None:
    old_ws = owner.get("ws")
    if old_ws is not None:
        try:
            await old_ws.close(code=4409, reason=_ws_close_reason("chat connection replaced by newer socket"))
        except Exception:
            pass
    old_bridge = owner.get("bridge")
    if old_bridge is not None:
        try:
            await asyncio.to_thread(old_bridge.close)
        except Exception:
            pass


def _latest_descendant(session_id: str) -> str | None:
    """Return the owner-local latest descendant for a resume id when available."""
    try:
        from hermes_state import SessionDB

        db = SessionDB()
        try:
            sid = db.resolve_session_id(session_id)
            if not sid or not db.get_session(sid):
                return None
            try:
                return db.resolve_resume_session_id(sid) or sid
            except Exception:
                return sid
        finally:
            db.close()
    except Exception:
        return None


def _build_gateway_ws_url(app_obj: Any) -> Optional[str]:
    owner_key = _owner_key(app_obj)
    base = os.environ.get("HERMES_OWNER_WORKER_CONTROL_WS_BASE", "").strip()
    if not owner_key or not base:
        return None
    qs = urllib_parse.urlencode(
        {
            "internal_owner_token": mint_internal_token(
                owner_key,
                audience=AUD_CONTROL_PLANE_WS,
                path="/api/ws",
                ttl_seconds=child_token_ttl_seconds(),
                control_home=_control_home(app_obj),
            )
        }
    )
    return f"{base.rstrip('/')}/api/ws?{qs}"


def _build_sidecar_url(app_obj: Any, channel: str) -> Optional[str]:
    owner_key = _owner_key(app_obj)
    base = os.environ.get("HERMES_OWNER_WORKER_CONTROL_WS_BASE", "").strip()
    if not owner_key or not base:
        return None
    qs = urllib_parse.urlencode(
        {
            "internal_owner_token": mint_internal_token(
                owner_key,
                audience=AUD_CONTROL_PLANE_WS,
                path="/api/pub",
                ttl_seconds=child_token_ttl_seconds(),
                control_home=_control_home(app_obj),
            ),
            "channel": channel,
        }
    )
    return f"{base.rstrip('/')}/api/pub?{qs}"


def _resolve_chat_argv(
    *,
    resume: Optional[str] = None,
    sidecar_url: Optional[str] = None,
    active_session_file: Optional[str] = None,
    browser_id: Optional[str] = None,
    app_obj: Any,
) -> tuple[list[str], Optional[str], dict[str, str]]:
    from hermes_cli.main import PROJECT_ROOT, _make_tui_argv

    tui_dir = PROJECT_ROOT / "ui-tui"
    prebuilt_tui_dir = tui_dir if (tui_dir / "dist" / "entry.js").is_file() else None
    old_tui_dir = os.environ.get("HERMES_TUI_DIR")
    try:
        if prebuilt_tui_dir is not None:
            os.environ["HERMES_TUI_DIR"] = str(prebuilt_tui_dir)
        argv, cwd = _make_tui_argv(tui_dir, tui_dev=False)
    finally:
        if old_tui_dir is None:
            os.environ.pop("HERMES_TUI_DIR", None)
        else:
            os.environ["HERMES_TUI_DIR"] = old_tui_dir

    env = os.environ.copy()
    try:
        from hermes_cli.config import apply_terminal_config_to_env

        apply_terminal_config_to_env(env=env)
    except Exception:
        pass
    env.setdefault("NODE_ENV", "production")
    env.setdefault("HERMES_TUI_DISABLE_MOUSE", "1")
    env.setdefault("HERMES_TUI_INLINE", "1")
    env["HERMES_TUI_DASHBOARD"] = "1"

    try:
        cwd_path = resolve_workspace_cwd(None, create_default=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    env["HERMES_CWD"] = str(cwd_path)
    env["TERMINAL_CWD"] = str(cwd_path)

    if resume:
        resume = _latest_descendant(resume) or resume
        env["HERMES_TUI_RESUME"] = resume
    if sidecar_url:
        env["HERMES_TUI_SIDECAR_URL"] = sidecar_url
    if active_session_file:
        env["HERMES_TUI_ACTIVE_SESSION_FILE"] = active_session_file
    if browser_id:
        env["HERMES_TUI_BROWSER_ID"] = browser_id
    if gateway_ws_url := _build_gateway_ws_url(app_obj):
        env["HERMES_TUI_GATEWAY_URL"] = gateway_ws_url

    return list(argv), str(cwd_path if cwd_path else cwd) if (cwd_path or cwd) else None, env


async def _resolve_chat_argv_async(**kwargs: Any) -> tuple[list[str], Optional[str], dict[str, str]]:
    lock_app = kwargs["app_obj"]
    async with _get_chat_argv_lock(lock_app):
        return await asyncio.to_thread(_resolve_chat_argv, **kwargs)


async def _broadcast_event(app: Any, channel: str, payload: str) -> None:
    event_channels, event_lock = _get_event_state(app)
    async with event_lock:
        subs = list(event_channels.get(channel, ()))
    for sub in subs:
        try:
            await sub.send_text(payload)
        except Exception:
            pass


async def pty_ws(ws: WebSocket) -> None:
    if not await _require_owner_token_or_close(ws):
        return
    await ws.accept()

    if not _PTY_BRIDGE_AVAILABLE:
        await ws.send_text("\r\n\x1b[31mChat unavailable: PTY support is unavailable on this platform.\x1b[0m\r\n")
        await ws.close(code=1011)
        return

    resume = ws.query_params.get("resume") or None
    browser_id = _browser_id_or_none(ws)
    browser_registered = False
    browser_owner_id = uuid.uuid4().hex if browser_id else ""
    channel = _channel_or_none(ws)
    sidecar_url = _build_sidecar_url(ws.app, channel) if channel else None
    force_fresh = (ws.query_params.get("fresh") or "").strip().lower() in {"1", "true", "yes", "on"}
    active_session_file: Optional[Path] = None

    if browser_id:
        current_channel = channel or ""
        replaced_owner = await _register_browser_pty_owner(ws.app, browser_id=browser_id, channel=current_channel, owner_id=browser_owner_id, ws=ws)
        browser_registered = True
        if replaced_owner is not None:
            await _close_replaced_browser_pty(replaced_owner)

    if channel:
        active_session_file = _active_session_file_for_channel(ws.app, channel)
        if force_fresh:
            resume = None
            _forget_active_session_file(active_session_file)
        elif not resume:
            resume = _read_active_session_file(active_session_file)

    try:
        argv, cwd, env = await _resolve_chat_argv_async(
            resume=resume,
            sidecar_url=sidecar_url,
            active_session_file=str(active_session_file) if active_session_file is not None else None,
            browser_id=browser_id,
            app_obj=ws.app,
        )
    except HTTPException as exc:
        await ws.send_text(f"\r\n\x1b[31mChat unavailable: {exc.detail}\x1b[0m\r\n")
        if browser_registered and browser_id:
            await _release_browser_pty_owner(ws.app, browser_id=browser_id, owner_id=browser_owner_id)
        await ws.close(code=1011)
        return
    except SystemExit as exc:
        await ws.send_text(f"\r\n\x1b[31mChat unavailable: {exc}\x1b[0m\r\n")
        if browser_registered and browser_id:
            await _release_browser_pty_owner(ws.app, browser_id=browser_id, owner_id=browser_owner_id)
        await ws.close(code=1011)
        return

    if browser_registered and browser_id and not await _browser_pty_owner_is_current(ws.app, browser_id=browser_id, owner_id=browser_owner_id):
        await ws.close(code=4409, reason=_ws_close_reason("chat connection replaced before spawn"))
        return

    try:
        bridge = await asyncio.to_thread(PtyBridge.spawn, argv, cwd=cwd, env=env)  # type: ignore[union-attr]
    except PtyUnavailableError as exc:
        await ws.send_text(f"\r\n\x1b[31mChat unavailable: {exc}\x1b[0m\r\n")
        if browser_registered and browser_id:
            await _release_browser_pty_owner(ws.app, browser_id=browser_id, owner_id=browser_owner_id)
        await ws.close(code=1011)
        return
    except (FileNotFoundError, OSError) as exc:
        await ws.send_text(f"\r\n\x1b[31mChat failed to start: {exc}\x1b[0m\r\n")
        if browser_registered and browser_id:
            await _release_browser_pty_owner(ws.app, browser_id=browser_id, owner_id=browser_owner_id)
        await ws.close(code=1011)
        return

    if browser_registered and browser_id:
        attached = await _attach_browser_pty_bridge(ws.app, browser_id=browser_id, owner_id=browser_owner_id, bridge=bridge)
        if not attached:
            await asyncio.to_thread(bridge.close)
            await ws.close(code=4409, reason=_ws_close_reason("chat connection replaced after spawn"))
            return

    loop = asyncio.get_running_loop()

    async def pump_pty_to_ws() -> None:
        try:
            while True:
                chunk = await loop.run_in_executor(None, bridge.read, _PTY_READ_CHUNK_TIMEOUT)
                if chunk is None:
                    return
                if not chunk:
                    await asyncio.sleep(0)
                    continue
                try:
                    await ws.send_bytes(chunk)
                except Exception:
                    return
        finally:
            try:
                await asyncio.to_thread(bridge.close)
            except Exception:
                pass
            try:
                await ws.close()
            except Exception:
                pass

    reader_task = asyncio.create_task(pump_pty_to_ws())
    try:
        while True:
            try:
                msg = await ws.receive()
            except RuntimeError:
                break
            if msg.get("type") == "websocket.disconnect":
                break
            raw = msg.get("bytes")
            if raw is None:
                text = msg.get("text")
                raw = text.encode("utf-8") if isinstance(text, str) else b""
            if not raw:
                continue
            match = _RESIZE_RE.match(raw)
            if match and match.end() == len(raw):
                bridge.resize(cols=int(match.group(1)), rows=int(match.group(2)))
                continue
            bridge.write(raw)
    except WebSocketDisconnect:
        pass
    finally:
        reader_task.cancel()
        try:
            await reader_task
        except (asyncio.CancelledError, Exception):
            pass
        await asyncio.to_thread(bridge.close)
        if browser_registered and browser_id:
            await _release_browser_pty_owner(ws.app, browser_id=browser_id, owner_id=browser_owner_id)


async def gateway_ws(ws: WebSocket) -> None:
    if not await _require_owner_token_or_close(ws):
        return
    from tui_gateway.ws import handle_ws

    await handle_ws(ws)


async def pub_ws(ws: WebSocket) -> None:
    if not await _require_owner_token_or_close(ws):
        return
    channel = _channel_or_none(ws)
    if not channel:
        await ws.close(code=4400)
        return
    await ws.accept()
    try:
        while True:
            await _broadcast_event(ws.app, channel, await ws.receive_text())
    except WebSocketDisconnect:
        pass


async def events_ws(ws: WebSocket) -> None:
    if not await _require_owner_token_or_close(ws):
        return
    channel = _channel_or_none(ws)
    if not channel:
        await ws.close(code=4400)
        return
    await ws.accept()
    event_channels, event_lock = _get_event_state(ws.app)
    async with event_lock:
        event_channels.setdefault(channel, set()).add(ws)
    try:
        while True:
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


def register_owner_worker_ws_routes(app: Any) -> None:
    """Register owner-worker-local WebSocket routes on *app*."""
    app.add_api_websocket_route("/api/pty", pty_ws)
    app.add_api_websocket_route("/api/ws", gateway_ws)
    app.add_api_websocket_route("/api/pub", pub_ws)
    app.add_api_websocket_route("/api/events", events_ws)
