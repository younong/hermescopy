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
import secrets
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib import parse as urllib_parse

from fastapi import HTTPException, WebSocket, WebSocketDisconnect

from hermes_cli.controlled_roots import ExpectedType, RootKind
from hermes_cli.dashboard_auth.audit import (
    AuthorityAuditEvent,
    AuthorityAuditReason,
    audit_authority,
    new_authority_correlation_id,
)
from hermes_cli.owner_runtime import resolve_workspace_cwd
from hermes_cli.dashboard_auth.authority import AuthorityStore
from hermes_cli.owner_worker.tokens import (
    OwnerWorkerCapabilityInvalid,
    admit_owner_worker_bootstrap,
    owp1_ack,
    owp1_data,
    parse_owp1_data,
    validate_owp1_control,
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
_OWNER_TUI_ATTACH_TTL_SECONDS = 60


class OwnerWorkerLiveState:
    """App-local authenticated browser, PTY, and event state."""

    def __init__(self) -> None:
        self.event_channels: dict[str, set[WebSocket]] = {}
        self.event_lock = asyncio.Lock()
        self.chat_argv_lock = asyncio.Lock()
        self.pty_active_session_files: dict[str, tuple[str, str]] = {}
        self.pty_browser_sessions: dict[str, dict[str, Any]] = {}
        self.pty_browser_lock = asyncio.Lock()
        # Bound by create_app after the exact worker lease is available.  The
        # Gateway route rejects rather than falling back to standalone globals
        # when this worker-local binding is absent.
        self.gateway_runtime: Any | None = None
        self.gateway_attach_tokens: dict[str, float] = {}
        self.gateway_attach_lock = threading.Lock()


def _live_state(app: Any) -> OwnerWorkerLiveState:
    try:
        state = app.state.owner_worker_live_state
    except AttributeError:
        state = OwnerWorkerLiveState()
        app.state.owner_worker_live_state = state
    return state


def _owner_key(app: Any) -> str:
    return str(getattr(app.state, "owner_worker_owner_key", "") or "").strip()


def _control_home(app: Any) -> str | Path | None:
    return getattr(app.state, "owner_worker_control_home", None)


def _ws_close_reason(text: str) -> str:
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= 123:
        return text
    return encoded[:120].decode("utf-8", "ignore") + "..."


class _Owp1Peer:
    """Expose framed UDS peer data as existing FastAPI WebSocket operations."""

    def __init__(self, ws: WebSocket, claims: Any) -> None:
        self._ws = ws
        self._claims = claims
        self._in_sequence = 1
        self._out_sequence = 1

    @property
    def claims(self) -> Any:
        """Immutable bootstrap claims trusted for this exact UDS connection."""
        return self._claims

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ws, name)

    async def accept(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    async def receive(self) -> dict[str, Any]:
        message = await self._ws.receive()
        if message.get("type") == "websocket.disconnect":
            return message
        framed = message.get("text")
        if framed is None:
            raise WebSocketDisconnect(code=4401)
        kind, payload = parse_owp1_data(
            framed,
            self._claims,
            direction="control-to-worker",
            expected_sequence=self._in_sequence,
        )
        self._in_sequence += 1
        return {"type": "websocket.receive", kind: payload}

    async def receive_text(self) -> str:
        message = await self.receive()
        if message.get("type") == "websocket.disconnect":
            raise WebSocketDisconnect(code=1000)
        if not isinstance(message.get("text"), str):
            raise WebSocketDisconnect(code=4401)
        return message["text"]

    async def send_text(self, data: str) -> None:
        await self._ws.send_text(owp1_data(
            self._claims,
            direction="worker-to-control",
            sequence=self._out_sequence,
            text=str(data),
        ))
        self._out_sequence += 1

    async def send_bytes(self, data: bytes) -> None:
        await self._ws.send_text(owp1_data(
            self._claims,
            direction="worker-to-control",
            sequence=self._out_sequence,
            data=bytes(data),
        ))
        self._out_sequence += 1


def _audit_bootstrap(reason: AuthorityAuditReason, lease: Any | None) -> None:
    if lease is None:
        return
    try:
        audit_authority(
            (
                AuthorityAuditEvent.CAPABILITY_ADMITTED
                if reason is AuthorityAuditReason.ADMITTED
                else AuthorityAuditEvent.CAPABILITY_REJECTED
            ),
            correlation_id=new_authority_correlation_id(),
            reason=reason,
            audience_class="none",
            worker_generation=int(lease.worker_generation),
            recovery_generation=int(lease.recovery_generation),
        )
    except Exception:
        return


async def _admit_bootstrap_or_close(ws: WebSocket) -> _Owp1Peer | None:
    """Consume one bootstrap and complete `owp1` hello/ack before route work."""
    token = ws.query_params.get("internal_owner_bootstrap", "")
    lease = getattr(ws.app.state, "owner_worker_lease", None)
    verifier = getattr(ws.app.state, "owner_worker_capability_verifier", {})
    if not token or lease is None:
        _audit_bootstrap(AuthorityAuditReason.BOOTSTRAP_REJECTED, lease)
        await ws.close(code=4401, reason=_ws_close_reason("auth: internal_owner_invalid"))
        return False
    try:
        claims = admit_owner_worker_bootstrap(
            token,
            expected_lease=lease,
            path=ws.url.path,
            authority_store=AuthorityStore(_control_home(ws.app)),
            public_key=verifier.get("HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY"),
            issuer_key_version=verifier.get("HERMES_OWNER_WORKER_CAPABILITY_ISSUER"),
            retained_public_keys=verifier.get("HERMES_OWNER_WORKER_CAPABILITY_RETAINED_PUBLIC_KEYS"),
        )
        await ws.accept()
        hello = await asyncio.wait_for(ws.receive_text(), timeout=5)
        validate_owp1_control(hello, claims, message_type="hello")
        await ws.send_text(owp1_ack(claims))
        _audit_bootstrap(AuthorityAuditReason.ADMITTED, lease)
        return _Owp1Peer(ws, claims)
    except (OwnerWorkerCapabilityInvalid, RuntimeError, TimeoutError):
        _audit_bootstrap(AuthorityAuditReason.BOOTSTRAP_REJECTED, lease)
        await ws.close(code=4401, reason=_ws_close_reason("auth: internal_owner_invalid"))
        return None


def _get_event_state(app: Any) -> tuple[dict[str, set[WebSocket]], asyncio.Lock]:
    state = _live_state(app)
    return state.event_channels, state.event_lock


def _get_chat_argv_lock(app: Any) -> asyncio.Lock:
    return _live_state(app).chat_argv_lock


def _get_pty_browser_state(app: Any) -> tuple[dict[str, dict[str, Any]], asyncio.Lock]:
    state = _live_state(app)
    return state.pty_browser_sessions, state.pty_browser_lock


def _get_pty_active_session_files(app: Any) -> dict[str, tuple[str, str]]:
    return _live_state(app).pty_active_session_files


def _mint_gateway_attach_token(app: Any) -> str:
    """Mint a single-use owner-worker-local credential for one PTY child."""
    state = _live_state(app)
    token = secrets.token_urlsafe(32)
    deadline = time.monotonic() + _OWNER_TUI_ATTACH_TTL_SECONDS
    with state.gateway_attach_lock:
        stale = [value for value, expires_at in state.gateway_attach_tokens.items() if expires_at <= time.monotonic()]
        for value in stale:
            state.gateway_attach_tokens.pop(value, None)
        state.gateway_attach_tokens[token] = deadline
    return token


def _consume_gateway_attach_token(app: Any, token: str) -> bool:
    state = _live_state(app)
    with state.gateway_attach_lock:
        expires_at = state.gateway_attach_tokens.pop(token, None)
    return expires_at is not None and expires_at > time.monotonic()


def _owner_tui_gateway_url(app: Any) -> str:
    """Return a private UDS attach URL for the PTY child owned by *app*."""
    return f"ws://owner-worker/api/ws?owner_tui_attach={_mint_gateway_attach_token(app)}"


def _trusted_live_metadata(peer: _Owp1Peer, path: str) -> tuple[str, int, str, int, int, str, str, str]:
    """Freeze the exact trusted admission fence for a worker live record."""
    claims = peer.claims
    expected_path = str(path).split("?", 1)[0]
    if claims.path != expected_path:
        raise OwnerWorkerCapabilityInvalid("live_state_path_mismatch")
    return (
        claims.owner_key,
        claims.worker_generation,
        claims.worker_id,
        claims.lease_version,
        claims.recovery_generation,
        claims.audience,
        claims.scope,
        claims.path,
    )


def _browser_id_or_none(ws: WebSocket) -> Optional[str]:
    browser_id = ws.query_params.get("browser_id", "")
    return browser_id if _VALID_BROWSER_ID_RE.match(browser_id) else None


def _channel_or_none(ws: WebSocket) -> Optional[str]:
    channel = ws.query_params.get("channel", "")
    return channel if _VALID_CHANNEL_RE.match(channel) else None


def _active_session_file_for_channel(app: Any, channel: str) -> str:
    """Create one owner-local active-session record under the temporary root.

    The TUI requires a path-valued environment variable, so this returns a
    descriptor-derived diagnostic location only after the controlled root has
    created the file.  It is never accepted back as filesystem authority.
    """
    files = _get_pty_active_session_files(app)
    existing = files.get(channel)
    if existing is not None:
        return existing[1]

    roots = getattr(getattr(app, "state", None), "owner_worker_controlled_roots", None)
    if roots is None:
        raise RuntimeError("authenticated owner worker requires controlled roots")
    relative_path = f"pty-active-sessions/{secrets.token_hex(16)}.json"
    roots.replace_bytes(RootKind.TEMPORARY, relative_path, b"{}", overwrite=False)
    # This is strictly child-process diagnostics. Subsequent owner-worker
    # operations recover the capability-relative name from app state, never by
    # parsing this pathname as an authorization input.
    path = str(roots.get(RootKind.TEMPORARY).canonical_path / relative_path)
    files[channel] = (relative_path, path)
    return path


def _active_session_relative_path(app: Any, path: str) -> str:
    files = _get_pty_active_session_files(app)
    for relative_path, diagnostic_path in files.values():
        if diagnostic_path == path:
            return relative_path
    raise RuntimeError("active session record is not owned by this worker")


def _read_active_session_file(app: Any, path: str) -> Optional[str]:
    try:
        roots = getattr(getattr(app, "state", None), "owner_worker_controlled_roots", None)
        if roots is None:
            raise RuntimeError("authenticated owner worker requires controlled roots")
        fd = roots.open_relative(
            RootKind.TEMPORARY,
            _active_session_relative_path(app, path),
            expected_type=ExpectedType.REGULAR_FILE,
        )
        try:
            chunks: list[bytes] = []
            while chunk := os.read(fd, 64 * 1024):
                chunks.append(chunk)
        finally:
            os.close(fd)
        data = json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    session_id = str(data.get("session_id") or "").strip()
    return session_id or None


def _forget_active_session_file(app: Any, path: str) -> None:
    try:
        roots = getattr(getattr(app, "state", None), "owner_worker_controlled_roots", None)
        if roots is None:
            raise RuntimeError("authenticated owner worker requires controlled roots")
        roots.remove(RootKind.TEMPORARY, _active_session_relative_path(app, path))
    except (OSError, RuntimeError):
        pass


async def _register_browser_pty_owner(
    app: Any,
    *,
    browser_id: str,
    channel: str,
    owner_id: str,
    ws: WebSocket,
    metadata: tuple[str, int, str, int, int, str, str, str],
) -> Optional[dict[str, Any]]:
    browser_sessions, browser_lock = _get_pty_browser_state(app)
    async with browser_lock:
        previous = browser_sessions.get(browser_id)
        browser_sessions[browser_id] = {
            "channel": channel,
            "started_at": time.time(),
            "owner_id": owner_id,
            "ws": ws,
            "bridge": None,
            "metadata": metadata,
        }
        return previous


async def _browser_pty_owner_is_current(
    app: Any, *, browser_id: str, owner_id: str, metadata: tuple[str, int, str, int, int, str, str, str]
) -> bool:
    browser_sessions, browser_lock = _get_pty_browser_state(app)
    async with browser_lock:
        existing = browser_sessions.get(browser_id)
        return existing is not None and existing.get("owner_id") == owner_id and existing.get("metadata") == metadata


async def _attach_browser_pty_bridge(
    app: Any, *, browser_id: str, owner_id: str, bridge: Any, metadata: tuple[str, int, str, int, int, str, str, str]
) -> bool:
    browser_sessions, browser_lock = _get_pty_browser_state(app)
    async with browser_lock:
        existing = browser_sessions.get(browser_id)
        if existing is None or existing.get("owner_id") != owner_id or existing.get("metadata") != metadata:
            return False
        existing["bridge"] = bridge
        return True


async def _release_browser_pty_owner(
    app: Any, *, browser_id: str, owner_id: str, metadata: tuple[str, int, str, int, int, str, str, str]
) -> None:
    browser_sessions, browser_lock = _get_pty_browser_state(app)
    async with browser_lock:
        existing = browser_sessions.get(browser_id)
        if (
            existing is not None
            and existing.get("owner_id") == owner_id
            and existing.get("metadata") == metadata
        ):
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
    """Build the one-use private Gateway endpoint for an owner-worker PTY child."""
    if not bool(getattr(getattr(app_obj, "state", None), "owner_worker_mode", False)):
        return None
    return _owner_tui_gateway_url(app_obj)


def _build_sidecar_url(app_obj: Any, channel: str) -> Optional[str]:
    del app_obj, channel
    return None


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

    state = getattr(app_obj, "state", None)
    roots = getattr(state, "owner_worker_controlled_roots", None)
    if roots is None:
        # Direct unit construction retains the legacy resolver only outside an
        # authenticated worker. Production owner workers always have the root.
        try:
            cwd_path = resolve_workspace_cwd(None, create_default=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        try:
            cwd_fd = roots.open_relative(
                RootKind.WORKSPACE,
                "default",
                expected_type=ExpectedType.DIRECTORY,
            )
            try:
                cwd_path = Path(os.readlink(f"/proc/self/fd/{cwd_fd}"))
            finally:
                os.close(cwd_fd)
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail="default owner workspace is unavailable") from exc
    if roots is None:
        env["HERMES_CWD"] = str(cwd_path)
        env["TERMINAL_CWD"] = str(cwd_path)
    else:
        # The authenticated child gets its cwd from the directory descriptor;
        # never let a display/environment path become an alternate authority.
        env.pop("HERMES_CWD", None)
        env.pop("TERMINAL_CWD", None)

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
        env["HERMES_OWNER_WORKER_TUI_ATTACH"] = "1"

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
    peer = await _admit_bootstrap_or_close(ws)
    if peer is None:
        return
    ws = peer
    metadata = _trusted_live_metadata(peer, "/api/pty")
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
    active_session_file: Optional[str] = None

    if browser_id:
        current_channel = channel or ""
        replaced_owner = await _register_browser_pty_owner(ws.app, browser_id=browser_id, channel=current_channel, owner_id=browser_owner_id, ws=ws, metadata=metadata)
        browser_registered = True
        if replaced_owner is not None:
            await _close_replaced_browser_pty(replaced_owner)

    if channel:
        active_session_file = _active_session_file_for_channel(ws.app, channel)
        if force_fresh:
            resume = None
            _forget_active_session_file(ws.app, active_session_file)
        elif not resume:
            resume = _read_active_session_file(ws.app, active_session_file)

    try:
        argv, cwd, env = await _resolve_chat_argv_async(
            resume=resume,
            sidecar_url=sidecar_url,
            active_session_file=active_session_file,
            browser_id=browser_id,
            app_obj=ws.app,
        )
    except HTTPException as exc:
        await ws.send_text(f"\r\n\x1b[31mChat unavailable: {exc.detail}\x1b[0m\r\n")
        if browser_registered and browser_id:
            await _release_browser_pty_owner(ws.app, browser_id=browser_id, owner_id=browser_owner_id, metadata=metadata)
        await ws.close(code=1011)
        return
    except SystemExit as exc:
        await ws.send_text(f"\r\n\x1b[31mChat unavailable: {exc}\x1b[0m\r\n")
        if browser_registered and browser_id:
            await _release_browser_pty_owner(ws.app, browser_id=browser_id, owner_id=browser_owner_id, metadata=metadata)
        await ws.close(code=1011)
        return

    if browser_registered and browser_id and not await _browser_pty_owner_is_current(
        ws.app, browser_id=browser_id, owner_id=browser_owner_id, metadata=metadata
    ):
        await ws.close(code=4409, reason=_ws_close_reason("chat connection replaced before spawn"))
        return

    cwd_fd: int | None = None
    try:
        roots = getattr(ws.app.state, "owner_worker_controlled_roots", None)
        if roots is not None:
            cwd_fd = roots.open_relative(
                RootKind.WORKSPACE,
                "default",
                expected_type=ExpectedType.DIRECTORY,
            )
            bridge = await asyncio.to_thread(PtyBridge.spawn, argv, cwd_fd=cwd_fd, env=env)  # type: ignore[union-attr]
        else:
            bridge = await asyncio.to_thread(PtyBridge.spawn, argv, cwd=cwd, env=env)  # type: ignore[union-attr]
    except PtyUnavailableError as exc:
        await ws.send_text(f"\r\n\x1b[31mChat unavailable: {exc}\x1b[0m\r\n")
        if browser_registered and browser_id:
            await _release_browser_pty_owner(ws.app, browser_id=browser_id, owner_id=browser_owner_id, metadata=metadata)
        await ws.close(code=1011)
        return
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        await ws.send_text(f"\r\n\x1b[31mChat unavailable: {exc}\x1b[0m\r\n")
        if browser_registered and browser_id:
            await _release_browser_pty_owner(ws.app, browser_id=browser_id, owner_id=browser_owner_id, metadata=metadata)
        await ws.close(code=1011)
        return
    finally:
        if cwd_fd is not None:
            os.close(cwd_fd)

    if browser_registered and browser_id:
        attached = await _attach_browser_pty_bridge(
            ws.app, browser_id=browser_id, owner_id=browser_owner_id, bridge=bridge, metadata=metadata
        )
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
            await _release_browser_pty_owner(ws.app, browser_id=browser_id, owner_id=browser_owner_id, metadata=metadata)


async def gateway_ws(ws: WebSocket) -> None:
    """Attach either the exact Control Plane bridge or one owner-local TUI child."""
    attach_token = str(ws.query_params.get("owner_tui_attach") or "")
    if attach_token:
        if not _consume_gateway_attach_token(ws.app, attach_token):
            await ws.close(code=4401, reason=_ws_close_reason("auth: owner_tui_attach_invalid"))
            return
        peer: WebSocket | _Owp1Peer = ws
    else:
        peer = await _admit_bootstrap_or_close(ws)
        if peer is None:
            return
    runtime = _live_state(ws.app).gateway_runtime
    if runtime is None:
        await peer.close(code=1011, reason=_ws_close_reason("owner gateway runtime unavailable"))
        return
    from tui_gateway.ws import handle_ws

    await handle_ws(peer, runtime=runtime, require_owner_runtime=True)


async def pub_ws(ws: WebSocket) -> None:
    if not await _admit_bootstrap_or_close(ws):
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
    if not await _admit_bootstrap_or_close(ws):
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
