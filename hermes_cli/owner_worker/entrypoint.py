"""Owner Worker process entrypoint.

This module intentionally sets and validates owner environment before importing
owner-sensitive modules such as ``hermes_state``.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import StreamingResponse


_IMAGE_PREVIEW_MAX_BYTES = 16 * 1024 * 1024


class BulkDeleteSessions(BaseModel):
    ids: list[str]
    profile: str | None = None


class SessionRename(BaseModel):
    title: str | None = None
    archived: bool | None = None
    profile: str | None = None


class SessionPrune(BaseModel):
    older_than_days: int = 90
    source: str | None = None
    profile: str | None = None


class ManagedFileUpload(BaseModel):
    path: str
    data_url: str
    overwrite: bool = True


class ManagedDirectoryCreate(BaseModel):
    path: str


class ManagedFileDelete(BaseModel):
    path: str
    recursive: bool = False


class SkillToggle(BaseModel):
    name: str
    enabled: bool
    profile: str | None = None


class SkillCreate(BaseModel):
    name: str
    content: str
    category: str | None = None
    profile: str | None = None


class SkillContentUpdate(BaseModel):
    name: str
    content: str
    profile: str | None = None


from hermes_cli.dashboard_auth.authority import (
    AuthorityStore,
    OwnerWorkerAuthorityLease,
    WorkerGenerationState,
    WorkerLeaseState,
)
from hermes_cli.controlled_roots import ControlledRoots, ExpectedType, RootKind, controlled_roots_for
from hermes_cli.owner_runtime import (
    assert_owner_runtime_paths,
    ensure_owner_runtime_dirs,
    owner_worker_env_for,
    owner_worker_socket_path,
    validate_owner_worker_runtime_environment,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Hermes owner worker")
    parser.add_argument("--owner-key", required=True)
    parser.add_argument("--owner-home", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--tenant-id", default="")
    parser.add_argument("--owner-user-id", default="")
    parser.add_argument("--auth-provider", default="")
    parser.add_argument("--control-home", default="")
    parser.add_argument("--worker-generation", required=True, type=int)
    parser.add_argument("--worker-id", required=True)
    return parser.parse_args()


def _worker_lease_from_env(owner_key: str) -> OwnerWorkerAuthorityLease:
    try:
        lease = OwnerWorkerAuthorityLease(
            owner_key=owner_key,
            worker_generation=int(os.environ["HERMES_WORKER_GENERATION"]),
            worker_id=str(os.environ["HERMES_WORKER_ID"]),
            state=WorkerLeaseState.STARTING,
            lease_version=int(os.environ["HERMES_WORKER_LEASE_VERSION"]),
            recovery_generation=int(os.environ["HERMES_WORKER_RECOVERY_GENERATION"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("owner worker lease environment is incomplete") from exc
    if (
        lease.worker_generation < 1
        or lease.lease_version < 1
        or lease.recovery_generation < 0
        or not lease.worker_id
    ):
        raise RuntimeError("owner worker lease environment is invalid")
    return lease


def _prepare_owner_env(args: argparse.Namespace) -> tuple[str, Path, Path]:
    owner_key = str(args.owner_key).strip()
    worker_generation = int(args.worker_generation)
    worker_id = str(args.worker_id).strip()
    if not owner_key or worker_generation < 1 or not worker_id:
        raise SystemExit("owner_key, worker_generation, and worker_id are required")
    owner_home = Path(args.owner_home).expanduser().resolve()
    socket_path = Path(args.socket).expanduser().resolve()
    if socket_path != owner_worker_socket_path(owner_home, worker_generation):
        raise SystemExit("worker socket does not match owner generation")

    existing_home = os.environ.get("HERMES_HOME", "").strip()
    if existing_home and Path(existing_home).expanduser().resolve() != owner_home:
        raise SystemExit("HERMES_HOME does not match owner_home")
    existing_owner = os.environ.get("HERMES_OWNER_KEY", "").strip()
    if existing_owner and existing_owner != owner_key:
        raise SystemExit("HERMES_OWNER_KEY does not match owner_key")
    existing_generation = os.environ.get("HERMES_WORKER_GENERATION", "").strip()
    if existing_generation and existing_generation != str(worker_generation):
        raise SystemExit("HERMES_WORKER_GENERATION does not match worker_generation")
    existing_worker_id = os.environ.get("HERMES_WORKER_ID", "").strip()
    if existing_worker_id and existing_worker_id != worker_id:
        raise SystemExit("HERMES_WORKER_ID does not match worker_id")

    ensure_owner_runtime_dirs(owner_home)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    bootstrap_env = owner_worker_env_for(
        owner_key=owner_key,
        owner_home=owner_home,
        tenant_id=str(args.tenant_id or ""),
        owner_user_id=str(args.owner_user_id or ""),
        auth_provider=str(args.auth_provider or ""),
        control_home=args.control_home or None,
        worker_generation=worker_generation,
        worker_id=worker_id,
    )
    os.environ.update(bootstrap_env)
    try:
        validate_owner_worker_runtime_environment(
            owner_home=owner_home,
            owner_key=owner_key,
            worker_generation=worker_generation,
            worker_id=worker_id,
            socket_path=socket_path,
        )
    except RuntimeError as exc:
        raise SystemExit(f"owner worker runtime admission failed: {exc}") from exc
    return owner_key, owner_home, socket_path


def create_app(
    owner_key: str,
    owner_home: Path,
    *,
    worker_generation: int = 1,
    worker_id: str = "direct-test-worker",
    socket_path: Path | None = None,
):
    if int(worker_generation) < 1 or not str(worker_id).strip():
        raise ValueError("worker_generation and worker_id are required")
    owner_home = Path(owner_home).expanduser().resolve()
    # Direct in-process construction is retained only for isolated unit tests.
    # Production entrypoint calls have already received this exact lease/env from
    # the supervisor before imports. It never reconstructs a lease here.
    fallback_lease: OwnerWorkerAuthorityLease | None = None
    fallback_verifier: dict[str, str] | None = None
    existing_home = os.environ.get("HERMES_HOME", "").strip()
    if existing_home and Path(existing_home).expanduser().resolve() != owner_home:
        raise RuntimeError("owner worker startup self-check failed: HERMES_HOME does not match owner_home")
    existing_owner = os.environ.get("HERMES_OWNER_KEY", "").strip()
    if existing_owner and existing_owner != owner_key:
        raise RuntimeError("owner worker startup self-check failed: HERMES_OWNER_KEY does not match owner_key")
    # Production passes the canonical UDS path and must use the supervisor's
    # complete lease environment. Direct app construction is isolated-test-only
    # and deliberately obtains a fresh complete lease instead of reusing ambient
    # state left by another in-process test app.
    if socket_path is None:
        control_home = os.environ.get("HERMES_CONTROL_HOME", "").strip()
        if not control_home:
            raise RuntimeError("owner worker control home is required")
        store = AuthorityStore(control_home)
        claim = store.claim_worker_start(owner_key, worker_id=worker_id)
        fallback_lease = store.transition_worker_lease(
            claim.lease,
            state=WorkerLeaseState.ACTIVE,
            generation_state=WorkerGenerationState.ACTIVE,
        )
        from .tokens import owner_worker_capability_public_config

        fallback_verifier = owner_worker_capability_public_config(control_home)
        os.environ.update(
            owner_worker_env_for(
                owner_key=owner_key,
                owner_home=owner_home,
                control_home=control_home,
                worker_generation=fallback_lease.worker_generation,
                worker_id=fallback_lease.worker_id,
                lease_version=fallback_lease.lease_version,
                recovery_generation=fallback_lease.recovery_generation,
                capability_issuer=fallback_verifier["HERMES_OWNER_WORKER_CAPABILITY_ISSUER"],
                capability_public_key=fallback_verifier["HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY"],
                capability_retained_public_keys=fallback_verifier[
                    "HERMES_OWNER_WORKER_CAPABILITY_RETAINED_PUBLIC_KEYS"
                ],
            )
        )
        worker_generation = fallback_lease.worker_generation
        worker_id = fallback_lease.worker_id
    try:
        runtime_paths = validate_owner_worker_runtime_environment(
            owner_home=owner_home,
            owner_key=owner_key,
            worker_generation=worker_generation,
            worker_id=worker_id,
            socket_path=socket_path,
        )
    except RuntimeError as exc:
        raise RuntimeError(f"owner worker startup self-check failed: {exc}") from exc
    try:
        controlled_roots = controlled_roots_for(runtime_paths)
    except (OSError, RuntimeError) as exc:
        raise RuntimeError(f"owner worker startup self-check failed: {exc}") from exc

    from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
    from fastapi.responses import JSONResponse

    @asynccontextmanager
    async def _lifespan(_: FastAPI):
        relay = None
        relay_fd = os.environ.pop("HERMES_DEPLOYMENT_INFERENCE_RELAY_FD", "").strip()
        if relay_fd:
            try:
                from hermes_cli.owner_worker.inference_relay import OwnerInferenceRelay

                relay = OwnerInferenceRelay(int(relay_fd))
                relay.start()
                os.environ["HERMES_DEPLOYMENT_INFERENCE_RELAY_BASE_URL"] = relay.base_url
            except Exception as exc:
                raise RuntimeError("deployment inference relay startup failed") from exc
        try:
            yield
        finally:
            os.environ.pop("HERMES_DEPLOYMENT_INFERENCE_RELAY_BASE_URL", None)
            if relay is not None:
                relay.close()
            supervisor = getattr(app.state, "tool_executor_supervisor", None)
            if supervisor is not None:
                supervisor.stop_generation()
            broker = getattr(app.state, "tool_executor_credential_broker", None)
            if broker is not None:
                broker.close()
            controlled_roots.close()
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB, get_default_db_path

    from hermes_cli import session_api

    from .tokens import (
        AUD_OWNER_WORKER_HTTP,
        SCOPE_OWNER_WORKER_HTTP,
        OwnerWorkerCapabilityInvalid,
        verify_owner_worker_capability,
    )
    app = FastAPI(title="Hermes Owner Worker", lifespan=_lifespan)
    app.state.owner_worker_mode = True
    app.state.owner_worker_owner_key = owner_key
    app.state.owner_worker_owner_home = owner_home
    app.state.owner_worker_generation = worker_generation
    app.state.owner_worker_id = worker_id
    app.state.owner_worker_controlled_roots = controlled_roots
    app.state.owner_worker_control_home = os.environ.get("HERMES_CONTROL_HOME", "") or None
    app.state.owner_worker_lease = fallback_lease or _worker_lease_from_env(owner_key)
    app.state.owner_worker_capability_verifier = fallback_verifier or {
        "HERMES_OWNER_WORKER_CAPABILITY_ISSUER": os.environ.get("HERMES_OWNER_WORKER_CAPABILITY_ISSUER", ""),
        "HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY": os.environ.get("HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY", ""),
        "HERMES_OWNER_WORKER_CAPABILITY_RETAINED_PUBLIC_KEYS": os.environ.get(
            "HERMES_OWNER_WORKER_CAPABILITY_RETAINED_PUBLIC_KEYS", "{}"
        ),
    }
    app.state.auth_required = False
    # Long-lived browser/PTY/event records are allocated once per worker app,
    # never in the Control Plane or a module-level singleton.
    from hermes_cli.owner_worker.ws_routes import OwnerWorkerLiveState
    from tui_gateway.server import OwnerWorkerGatewayRuntime

    app.state.owner_worker_live_state = OwnerWorkerLiveState()
    lease = app.state.owner_worker_lease
    from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext

    workspace_context = AuthenticatedWorkspaceContext(controlled_roots)
    from hermes_cli.owner_worker.audit import report_executor_authority_decision
    from hermes_cli.owner_worker.credential_broker import CredentialBroker
    from hermes_cli.owner_worker.tool_executor_sandbox import load_sandbox_deployment_policy
    from hermes_cli.owner_worker.tool_executor_supervisor import ToolExecutorSupervisor

    app.state.tool_executor_credential_broker = CredentialBroker(
        audit_reporter=report_executor_authority_decision,
    )
    policy_factory = os.environ.get("HERMES_SANDBOX_DEPLOYMENT_POLICY", "")
    try:
        deployment_policy = load_sandbox_deployment_policy(policy_factory)
    except Exception:
        # The Gateway stays available for non-tool work, while tool admission
        # remains fail closed until a deployment operator supplies this policy.
        app.state.tool_executor_supervisor = None
        app.state.tool_executor_startup_error = "sandbox deployment policy unavailable"
    else:
        app.state.tool_executor_supervisor = ToolExecutorSupervisor(
            owner_home=owner_home,
            workspace_context=workspace_context,
            lease=lease,
            credential_broker=app.state.tool_executor_credential_broker,
            deployment_policy=deployment_policy,
            control_home=app.state.owner_worker_control_home,
            audit_reporter=report_executor_authority_decision,
        )
    app.state.owner_worker_live_state.gateway_runtime = OwnerWorkerGatewayRuntime(
        owner_key=lease.owner_key,
        worker_generation=lease.worker_generation,
        worker_id=lease.worker_id,
        lease_version=lease.lease_version,
        recovery_generation=lease.recovery_generation,
        filesystem_context=workspace_context,
        tool_executor_supervisor=app.state.tool_executor_supervisor,
    )

    def _reject_profile(profile: str | None) -> None:
        if profile and str(profile).strip().lower() not in {"default"}:
            raise HTTPException(status_code=400, detail="profile selection is not available in authenticated mode")

    def _file_path(path: str | None, *, allow_empty: bool = False) -> str:
        value = str(path or "").strip()
        if allow_empty and not value:
            return ""
        if not value:
            raise HTTPException(status_code=400, detail="Path is required")
        try:
            app.state.owner_worker_controlled_roots._require_linux()
            components = value.split("/")
            if value.startswith("/") or "\x00" in value or any(part in {"", ".", ".."} for part in components):
                raise ValueError
        except (TypeError, ValueError, RuntimeError):
            raise HTTPException(status_code=400, detail="Path must be a relative workspace path")
        return value

    def _owner_image_path(path: str | None) -> str:
        value = str(path or "").strip()
        if not value or "\x00" in value:
            raise HTTPException(status_code=400, detail="Invalid image path")
        try:
            candidate = Path(value)
            if not candidate.is_absolute():
                raise ValueError
            relative_path = candidate.relative_to(owner_home).as_posix()
            components = relative_path.split("/")
            if (
                len(components) != 2
                or components[0] != "images"
                or any(part in {"", ".", ".."} for part in components)
            ):
                raise ValueError
            app.state.owner_worker_controlled_roots._require_linux()
        except (OSError, TypeError, ValueError, RuntimeError):
            raise HTTPException(
                status_code=400,
                detail="Image path must be in the owner images directory",
            )
        return relative_path

    def _file_entry(relative_path: str):
        roots = app.state.owner_worker_controlled_roots
        fd = roots.open_relative(RootKind.WORKSPACE, relative_path, expected_type=ExpectedType.REGULAR_FILE)
        try:
            metadata = os.fstat(fd)
        finally:
            os.close(fd)
        name = relative_path.rsplit("/", 1)[-1]
        return {
            "name": name,
            "path": relative_path,
            "is_directory": False,
            "size": metadata.st_size,
            "mtime": metadata.st_mtime,
            "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
        }

    def _decode_data_url(data_url: str) -> bytes:
        text = str(data_url or "").strip()
        if not text.startswith("data:") or "," not in text or ";base64" not in text.split(",", 1)[0]:
            raise HTTPException(status_code=400, detail="Upload payload must be a base64 data URL")
        try:
            data = base64.b64decode(text.split(",", 1)[1], validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(status_code=400, detail="Upload payload is not valid base64")
        if len(data) > 100 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="File is too large")
        return data

    def _files_error(exc: Exception) -> HTTPException:
        if isinstance(exc, FileNotFoundError):
            return HTTPException(status_code=404, detail="Path not found")
        if isinstance(exc, (PermissionError, RuntimeError, OSError)):
            return HTTPException(status_code=400, detail="Unsafe or invalid filesystem path")
        return HTTPException(status_code=500, detail="Filesystem operation failed")

    @app.middleware("http")
    async def _reject_external_owner_selectors(request: Request, call_next):
        for key in ("owner", "owner_home", "owner_key"):
            if str(request.query_params.get(key) or "").strip():
                return JSONResponse(
                    status_code=400,
                    content={"detail": "owner selection is not available in authenticated mode"},
                )
        return await call_next(request)

    def _require_owner_token(request: Request, authorization: str | None = Header(default=None)) -> None:
        token = ""
        if authorization and authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
        try:
            verify_owner_worker_capability(
                token,
                expected_lease=app.state.owner_worker_lease,
                audience=AUD_OWNER_WORKER_HTTP,
                scope=SCOPE_OWNER_WORKER_HTTP,
                path=request.url.path,
                authority_store=AuthorityStore(app.state.owner_worker_control_home),
                public_key=getattr(app.state, "owner_worker_capability_verifier", {}).get(
                    "HERMES_OWNER_WORKER_CAPABILITY_PUBLIC_KEY"
                ),
                issuer_key_version=getattr(app.state, "owner_worker_capability_verifier", {}).get(
                    "HERMES_OWNER_WORKER_CAPABILITY_ISSUER"
                ),
                retained_public_keys=getattr(app.state, "owner_worker_capability_verifier", {}).get(
                    "HERMES_OWNER_WORKER_CAPABILITY_RETAINED_PUBLIC_KEYS"
                ),
            )
        except (OwnerWorkerCapabilityInvalid, RuntimeError):
            raise HTTPException(status_code=401, detail="invalid owner worker capability") from None

    def _open_db() -> Any:
        return SessionDB()

    def _clear_skills_prompt_cache() -> None:
        try:
            from agent.prompt_builder import clear_skills_system_prompt_cache

            clear_skills_system_prompt_cache(clear_snapshot=True)
        except Exception:
            pass

    try:
        hermes_home = get_hermes_home().resolve()
        if hermes_home != owner_home:
            raise RuntimeError("get_hermes_home() does not match owner_home")
        if os.environ.get("HERMES_OWNER_KEY") != owner_key:
            raise RuntimeError("HERMES_OWNER_KEY does not match owner_key")
        configured_generation = os.environ.get("HERMES_WORKER_GENERATION", "").strip()
        if configured_generation and configured_generation != str(worker_generation):
            raise RuntimeError("HERMES_WORKER_GENERATION does not match worker_generation")
        configured_worker_id = os.environ.get("HERMES_WORKER_ID", "").strip()
        if configured_worker_id and configured_worker_id != worker_id:
            raise RuntimeError("HERMES_WORKER_ID does not match worker_id")
        if get_default_db_path().resolve() != (owner_home / "state.db").resolve():
            raise RuntimeError("SessionDB default path is not owner-local")
        db = SessionDB()
        try:
            if Path(db.db_path).resolve() != (owner_home / "state.db").resolve():
                raise RuntimeError("SessionDB resolved outside owner_home")
        finally:
            db.close()
        from tools import checkpoint_manager, process_registry
        from gateway import channel_directory, mirror

        assert_owner_runtime_paths(
            [
                ("checkpoints", checkpoint_manager._effective_checkpoint_base()),
                ("process_registry", process_registry._effective_checkpoint_path()),
                ("channel_directory", channel_directory._effective_directory_path()),
                ("channel_aliases", channel_directory._effective_channel_aliases_path()),
                ("sessions_index", channel_directory._effective_sessions_index_path()),
                ("mirror_sessions_index", mirror._effective_sessions_index_path()),
            ],
            expected_paths=runtime_paths,
        )
    except Exception as exc:
        controlled_roots.close()
        raise RuntimeError(f"owner worker startup self-check failed: {exc}") from exc

    @app.get("/internal/health")
    def health(_: None = Depends(_require_owner_token)) -> dict[str, Any]:
        from hermes_cli.owner_runtime import FORBIDDEN_OWNER_WORKER_ENV_KEYS, get_workspace_root

        return {
            "ready": True,
            "owner_key": owner_key,
            "owner_home": str(owner_home),
            "worker_generation": worker_generation,
            "worker_id": worker_id,
            "lease_version": app.state.owner_worker_lease.lease_version,
            "recovery_generation": app.state.owner_worker_lease.recovery_generation,
            "pid": os.getpid(),
            "hermes_home": str(get_hermes_home().resolve()),
            "workspace_root": str(get_workspace_root()),
            "control_home": str(app.state.owner_worker_control_home or ""),
            "forbidden_env_present": [key for key in FORBIDDEN_OWNER_WORKER_ENV_KEYS if os.environ.get(key, "").strip()],
        }

    @app.get("/api/files")
    def list_files(path: str = "", _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        relative_path = _file_path(path, allow_empty=True)
        try:
            entries = [
                {
                    "name": entry.name,
                    "path": entry.relative_path,
                    "is_directory": entry.is_directory,
                    "size": entry.size,
                    "mtime": entry.mtime,
                    "mime_type": None if entry.is_directory else mimetypes.guess_type(entry.name)[0] or "application/octet-stream",
                }
                for entry in app.state.owner_worker_controlled_roots.list_directory(RootKind.WORKSPACE, relative_path)
            ]
        except Exception as exc:
            raise _files_error(exc) from exc
        parent = None
        if relative_path:
            parent = relative_path.rsplit("/", 1)[0] if "/" in relative_path else ""
        return {"path": relative_path, "parent": parent, "entries": entries, "root": "", "locked_root": "", "can_change_path": False}

    @app.get("/api/files/read")
    def read_file(path: str, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        relative_path = _file_path(path)
        try:
            fd = app.state.owner_worker_controlled_roots.open_relative(
                RootKind.WORKSPACE, relative_path, expected_type=ExpectedType.REGULAR_FILE
            )
            try:
                metadata = os.fstat(fd)
                if metadata.st_size > 100 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="File is too large")
                data = bytearray()
                while len(data) <= 100 * 1024 * 1024:
                    chunk = os.read(fd, min(1024 * 1024, 100 * 1024 * 1024 + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
            finally:
                os.close(fd)
        except HTTPException:
            raise
        except Exception as exc:
            raise _files_error(exc) from exc
        name = relative_path.rsplit("/", 1)[-1]
        mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return {"name": name, "path": relative_path, "size": len(data), "mime_type": mime_type, "data_url": f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}", "root": "", "locked_root": "", "can_change_path": False}

    @app.get("/api/fs/read-data-url")
    def read_image_data_url(path: str, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        relative_path = _owner_image_path(path)
        try:
            fd = app.state.owner_worker_controlled_roots.open_relative(
                RootKind.OWNER_WRITABLE,
                relative_path,
                expected_type=ExpectedType.REGULAR_FILE,
            )
            try:
                metadata = os.fstat(fd)
                if metadata.st_size > _IMAGE_PREVIEW_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="File too large")
                data = bytearray()
                while chunk := os.read(
                    fd,
                    min(1024 * 1024, _IMAGE_PREVIEW_MAX_BYTES - len(data)),
                ):
                    data.extend(chunk)
            finally:
                os.close(fd)
        except HTTPException:
            raise
        except Exception as exc:
            raise _files_error(exc) from exc
        name = relative_path.rsplit("/", 1)[-1]
        mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return {"dataUrl": f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"}

    @app.get("/api/files/download")
    def download_file(path: str, _: None = Depends(_require_owner_token)):
        relative_path = _file_path(path)
        try:
            fd = app.state.owner_worker_controlled_roots.open_relative(
                RootKind.WORKSPACE, relative_path, expected_type=ExpectedType.REGULAR_FILE
            )
            metadata = os.fstat(fd)
            if metadata.st_size > 100 * 1024 * 1024:
                os.close(fd)
                raise HTTPException(status_code=413, detail="File is too large")
        except HTTPException:
            raise
        except Exception as exc:
            raise _files_error(exc) from exc

        def chunks():
            try:
                while chunk := os.read(fd, 1024 * 1024):
                    yield chunk
            finally:
                os.close(fd)

        name = relative_path.rsplit("/", 1)[-1]
        return StreamingResponse(
            chunks(),
            media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.post("/api/files/upload")
    def upload_file(payload: ManagedFileUpload, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        relative_path = _file_path(payload.path)
        try:
            app.state.owner_worker_controlled_roots.replace_bytes(
                RootKind.WORKSPACE, relative_path, _decode_data_url(payload.data_url), overwrite=payload.overwrite
            )
            entry = _file_entry(relative_path)
        except HTTPException:
            raise
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail="A file already exists at that path") from exc
        except Exception as exc:
            raise _files_error(exc) from exc
        return {"ok": True, "entry": entry, "path": relative_path, "root": "", "locked_root": "", "can_change_path": False}

    @app.post("/api/files/upload-stream")
    async def upload_file_stream(
        file: UploadFile = File(...), path: str = Form(...), overwrite: bool = Form(True), _: None = Depends(_require_owner_token)
    ) -> dict[str, Any]:
        relative_path = _file_path(path)
        writer = None
        try:
            writer = app.state.owner_worker_controlled_roots.begin_atomic_replace(
                RootKind.WORKSPACE,
                relative_path,
                overwrite=overwrite,
            )
            while chunk := await file.read(1024 * 1024):
                writer.write(chunk)
                if writer.bytes_written > 100 * 1024 * 1024:
                    raise HTTPException(status_code=413, detail="File is too large")
            writer.commit()
            writer = None
            entry = _file_entry(relative_path)
        except HTTPException:
            raise
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail="A file already exists at that path") from exc
        except Exception as exc:
            raise _files_error(exc) from exc
        finally:
            if writer is not None:
                writer.abort()
            await file.close()
        return {"ok": True, "entry": entry, "path": relative_path, "root": "", "locked_root": "", "can_change_path": False}

    @app.post("/api/files/mkdir")
    def create_directory(payload: ManagedDirectoryCreate, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        relative_path = _file_path(payload.path)
        try:
            app.state.owner_worker_controlled_roots.mkdirs(RootKind.WORKSPACE, relative_path)
            entries = app.state.owner_worker_controlled_roots.list_directory(RootKind.WORKSPACE, relative_path.rsplit("/", 1)[0] if "/" in relative_path else "")
            entry = next(item for item in entries if item.relative_path == relative_path)
        except Exception as exc:
            raise _files_error(exc) from exc
        return {"ok": True, "entry": {"name": entry.name, "path": entry.relative_path, "is_directory": True, "size": None, "mtime": entry.mtime, "mime_type": None}, "path": relative_path, "root": "", "locked_root": "", "can_change_path": False}

    @app.delete("/api/files")
    def delete_file(payload: ManagedFileDelete, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        relative_path = _file_path(payload.path)
        try:
            app.state.owner_worker_controlled_roots.remove(RootKind.WORKSPACE, relative_path, recursive=payload.recursive)
        except OSError as exc:
            raise HTTPException(status_code=409 if not payload.recursive else 400, detail="Could not delete path") from exc
        except Exception as exc:
            raise _files_error(exc) from exc
        return {"ok": True, "path": relative_path, "root": "", "locked_root": "", "can_change_path": False}

    @app.get("/api/sessions")
    def get_sessions(
        limit: int = 20,
        offset: int = 0,
        min_messages: int = 0,
        archived: str = "exclude",
        order: str = "created",
        source: str | None = None,
        exclude_sources: str | None = None,
        cwd_prefix: str | None = None,
        profile: str | None = None,
        _: None = Depends(_require_owner_token),
    ) -> dict[str, Any]:
        _reject_profile(profile)
        from gateway.session import current_historical_resume_scope

        db = _open_db()
        try:
            scope = current_historical_resume_scope()
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
                recovery_scope=(scope if socket_path is not None and scope is not None else None),
            )
        finally:
            db.close()

    @app.get("/api/sessions/search")
    def search_sessions(q: str = "", limit: int = 20, profile: str | None = None, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        _reject_profile(profile)
        db = _open_db()
        try:
            return session_api.search_sessions_payload(db, q=q, limit=limit)
        finally:
            db.close()

    @app.post("/api/sessions/bulk-delete")
    def bulk_delete_sessions(body: BulkDeleteSessions, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        _reject_profile(body.profile)
        db = _open_db()
        try:
            return session_api.bulk_delete_payload(db, body.ids)
        finally:
            db.close()

    @app.get("/api/sessions/empty/count")
    def count_empty_sessions(profile: str | None = None, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        _reject_profile(profile)
        db = _open_db()
        try:
            return session_api.empty_count_payload(db)
        finally:
            db.close()

    @app.delete("/api/sessions/empty")
    def delete_empty_sessions(profile: str | None = None, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        _reject_profile(profile)
        db = _open_db()
        try:
            return session_api.delete_empty_payload(db)
        finally:
            db.close()

    @app.get("/api/sessions/stats")
    def get_session_stats(profile: str | None = None, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        _reject_profile(profile)
        db = _open_db()
        try:
            return session_api.stats_payload(db)
        finally:
            db.close()

    @app.get("/api/analytics/usage")
    def get_usage_analytics(days: int = 30, profile: str | None = None, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        _reject_profile(profile)
        from hermes_cli.session_analytics import usage_analytics_from_db

        db = _open_db()
        try:
            return usage_analytics_from_db(db, days=days)
        finally:
            db.close()

    @app.get("/api/analytics/models")
    def get_models_analytics(days: int = 30, profile: str | None = None, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        _reject_profile(profile)
        from hermes_cli.session_analytics import models_analytics_from_db

        db = _open_db()
        try:
            return models_analytics_from_db(db, days=days)
        finally:
            db.close()

    @app.get("/api/profiles")
    def get_profiles(_: None = Depends(_require_owner_token)) -> dict[str, Any]:
        from hermes_cli.dashboard_owner_payloads import owner_singleton_profile_payload

        return owner_singleton_profile_payload(owner_home)

    @app.get("/api/config")
    def get_config(profile: str | None = None, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        _reject_profile(profile)
        from hermes_cli.config import load_config
        from hermes_cli.dashboard_owner_payloads import normalize_config_for_web

        return normalize_config_for_web(load_config())

    @app.get("/api/dashboard/font")
    def get_dashboard_font(_: None = Depends(_require_owner_token)) -> dict[str, str]:
        from hermes_cli.dashboard_owner_payloads import dashboard_font_payload

        return dashboard_font_payload()

    @app.get("/api/dashboard/plugins")
    def get_dashboard_plugins(_: None = Depends(_require_owner_token)) -> list[dict[str, Any]]:
        from hermes_cli.dashboard_owner_payloads import active_dashboard_plugin_payload

        return active_dashboard_plugin_payload()

    @app.get("/api/skills")
    def get_skills(profile: str | None = None, _: None = Depends(_require_owner_token)) -> list[dict[str, Any]]:
        _reject_profile(profile)
        from hermes_cli.config import load_config
        from hermes_cli.skills_config import get_disabled_skills
        from tools.skills_tool import _find_all_skills

        disabled = get_disabled_skills(load_config())
        skills = _find_all_skills(skip_disabled=True)
        for skill in skills:
            skill["enabled"] = skill["name"] not in disabled
        return skills

    @app.put("/api/skills/toggle")
    def toggle_skill(
        body: SkillToggle,
        profile: str | None = None,
        _: None = Depends(_require_owner_token),
    ) -> dict[str, Any]:
        _reject_profile(body.profile)
        _reject_profile(profile)
        from hermes_cli.config import load_config
        from hermes_cli.skills_config import get_disabled_skills, save_disabled_skills

        config = load_config()
        disabled = get_disabled_skills(config)
        if body.enabled:
            disabled.discard(body.name)
        else:
            disabled.add(body.name)
        save_disabled_skills(config, disabled)
        return {"ok": True, "name": body.name, "enabled": body.enabled}

    @app.get("/api/skills/content")
    def get_skill_content(
        name: str,
        profile: str | None = None,
        _: None = Depends(_require_owner_token),
    ) -> dict[str, Any]:
        _reject_profile(profile)
        from tools.skill_manager_tool import _find_skill

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
    def create_skill(body: SkillCreate, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        _reject_profile(body.profile)
        from tools.skill_manager_tool import _create_skill

        result = _create_skill(body.name, body.content, body.category or None)
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Failed to create skill."))
        _clear_skills_prompt_cache()
        return result

    @app.put("/api/skills/content")
    def update_skill_content(
        body: SkillContentUpdate,
        _: None = Depends(_require_owner_token),
    ) -> dict[str, Any]:
        _reject_profile(body.profile)
        from tools.skill_manager_tool import _edit_skill

        result = _edit_skill(body.name, body.content)
        if not result.get("success"):
            error = result.get("error", "Failed to update skill.")
            status = 404 if "not found" in str(error).lower() else 400
            raise HTTPException(status_code=status, detail=error)
        _clear_skills_prompt_cache()
        return result

    @app.get("/api/tools/toolsets")
    def get_toolsets(
        profile: str | None = None,
        _: None = Depends(_require_owner_token),
    ) -> list[dict[str, Any]]:
        _reject_profile(profile)
        from hermes_cli.dashboard_owner_payloads import toolsets_payload

        return toolsets_payload()

    @app.get("/api/model/info")
    def get_model_info(profile: str | None = None, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        _reject_profile(profile)
        from hermes_cli.config import load_config
        from hermes_cli.deployment_inference import deployment_descriptor_from_environment
        from hermes_cli.model_info_payload import model_info_payload_from_config

        return model_info_payload_from_config(
            load_config(),
            deployment_descriptor=deployment_descriptor_from_environment(),
        )

    @app.post("/api/sessions/prune")
    def prune_sessions(body: SessionPrune, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        _reject_profile(body.profile)
        db = _open_db()
        try:
            return session_api.prune_sessions_payload(
                db,
                older_than_days=body.older_than_days,
                source=body.source,
                sessions_dir=owner_home / "sessions",
            )
        finally:
            db.close()

    @app.get("/api/sessions/{session_id}/latest-descendant")
    def get_session_latest_descendant(session_id: str, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        from gateway.session import current_historical_resume_scope

        db = _open_db()
        try:
            scope = current_historical_resume_scope()
            # Direct in-process app construction is test-only and may create
            # legacy owner-local rows before the gateway writes complete durable
            # metadata. Production app construction receives a UDS socket from
            # the supervisor and always enforces the historical scope.
            if socket_path is None or scope is None:
                return session_api.latest_descendant_payload(db, session_id)
            return session_api.latest_descendant_payload(
                db,
                session_id,
                recovery_scope=scope,
            )
        finally:
            db.close()

    @app.get("/api/sessions/{session_id}/messages")
    def get_session_messages(session_id: str, profile: str | None = None, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        _reject_profile(profile)
        db = _open_db()
        try:
            return session_api.session_messages_payload(db, session_id)
        finally:
            db.close()

    @app.get("/api/sessions/{session_id}/export")
    def export_session(session_id: str, profile: str | None = None, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        _reject_profile(profile)
        db = _open_db()
        try:
            return session_api.export_session_payload(db, session_id)
        finally:
            db.close()

    @app.patch("/api/sessions/{session_id}")
    def rename_session(session_id: str, body: SessionRename, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        _reject_profile(body.profile)
        db = _open_db()
        try:
            return session_api.rename_session_payload(db, session_id, title=body.title, archived=body.archived)
        finally:
            db.close()

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str, profile: str | None = None, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        _reject_profile(profile)
        db = _open_db()
        try:
            return session_api.delete_session_payload(db, session_id)
        finally:
            db.close()

    @app.get("/api/sessions/{session_id}")
    def get_session_detail(session_id: str, profile: str | None = None, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        _reject_profile(profile)
        db = _open_db()
        try:
            return session_api.session_detail_payload(db, session_id)
        finally:
            db.close()

    # Register worker-local WebSocket handlers without importing the Control
    # Plane web_server module. This keeps owner-worker runtime state scoped to
    # this FastAPI app and avoids accidentally touching web_server.app globals.
    from hermes_cli.owner_worker.ws_routes import register_owner_worker_ws_routes

    register_owner_worker_ws_routes(app)

    return app


def main() -> None:
    args = _parse_args()
    owner_key, owner_home, socket_path = _prepare_owner_env(args)

    import uvicorn

    try:
        socket_path.unlink()
    except FileNotFoundError:
        pass
    app = create_app(
        owner_key,
        owner_home,
        worker_generation=int(args.worker_generation),
        worker_id=str(args.worker_id),
        socket_path=socket_path,
    )
    os.umask(0o077)
    uvicorn.run(app, uds=str(socket_path), log_level="warning", access_log=False)


if __name__ == "__main__":  # pragma: no cover
    main()
