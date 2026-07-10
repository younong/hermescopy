"""Owner Worker process entrypoint.

This module intentionally sets and validates owner environment before importing
owner-sensitive modules such as ``hermes_state``.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from starlette.requests import Request

from hermes_cli.owner_runtime import (
    assert_owner_runtime_paths,
    ensure_owner_runtime_dirs,
    owner_worker_env_for,
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
    return parser.parse_args()


def _prepare_owner_env(args: argparse.Namespace) -> tuple[str, Path, Path]:
    owner_key = str(args.owner_key).strip()
    if not owner_key:
        raise SystemExit("owner_key is required")
    owner_home = Path(args.owner_home).expanduser().resolve()
    socket_path = Path(args.socket).expanduser().resolve()
    if socket_path.parent != (owner_home / "runtime").resolve():
        raise SystemExit("worker socket must live under owner_home/runtime")

    ensure_owner_runtime_dirs(owner_home)

    existing_home = os.environ.get("HERMES_HOME", "").strip()
    if existing_home and Path(existing_home).expanduser().resolve() != owner_home:
        raise SystemExit("HERMES_HOME does not match owner_home")
    existing_owner = os.environ.get("HERMES_OWNER_KEY", "").strip()
    if existing_owner and existing_owner != owner_key:
        raise SystemExit("HERMES_OWNER_KEY does not match owner_key")

    os.environ.update(
        owner_worker_env_for(
            owner_key=owner_key,
            owner_home=owner_home,
            tenant_id=str(args.tenant_id or ""),
            owner_user_id=str(args.owner_user_id or ""),
            auth_provider=str(args.auth_provider or ""),
            control_home=args.control_home or None,
        )
    )

    return owner_key, owner_home, socket_path


def create_app(owner_key: str, owner_home: Path):
    from fastapi import Depends, FastAPI, Header, HTTPException, Request
    from fastapi.responses import JSONResponse
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB, get_default_db_path
    from pydantic import BaseModel

    from hermes_cli import session_api

    from .tokens import AUD_OWNER_WORKER_HTTP, validate_internal_token

    app = FastAPI(title="Hermes Owner Worker")
    app.state.owner_worker_mode = True
    app.state.owner_worker_owner_key = owner_key
    app.state.owner_worker_owner_home = owner_home
    app.state.owner_worker_control_home = os.environ.get("HERMES_CONTROL_HOME", "") or None
    app.state.auth_required = False

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

    def _reject_profile(profile: str | None) -> None:
        if profile and str(profile).strip().lower() not in {"default"}:
            raise HTTPException(status_code=400, detail="profile selection is not available in authenticated mode")

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
        if not validate_internal_token(
            token,
            owner_key,
            audience=AUD_OWNER_WORKER_HTTP,
            path=request.url.path,
            control_home=app.state.owner_worker_control_home,
        ):
            raise HTTPException(status_code=401, detail="invalid owner worker token")

    def _open_db() -> Any:
        return SessionDB()

    try:
        hermes_home = get_hermes_home().resolve()
        if hermes_home != owner_home:
            raise RuntimeError("get_hermes_home() does not match owner_home")
        if os.environ.get("HERMES_OWNER_KEY") != owner_key:
            raise RuntimeError("HERMES_OWNER_KEY does not match owner_key")
        if get_default_db_path().resolve() != (owner_home / "state.db").resolve():
            raise RuntimeError("SessionDB default path is not owner-local")
        db = SessionDB()
        try:
            if Path(db.db_path).resolve() != (owner_home / "state.db").resolve():
                raise RuntimeError("SessionDB resolved outside owner_home")
        finally:
            db.close()
        extra_paths: list[tuple[str, Path]] = []
        try:
            from tools import checkpoint_manager

            extra_paths.append(("checkpoint_base", checkpoint_manager._effective_checkpoint_base()))
        except Exception:
            pass
        try:
            from tools import process_registry

            extra_paths.append(("process_registry", process_registry._effective_checkpoint_path()))
        except Exception:
            pass
        try:
            from gateway import channel_directory

            extra_paths.extend(
                [
                    ("channel_directory", channel_directory._effective_directory_path()),
                    ("channel_aliases", channel_directory._effective_channel_aliases_path()),
                    ("sessions_index", channel_directory._effective_sessions_index_path()),
                ]
            )
        except Exception:
            pass
        try:
            from gateway import mirror

            extra_paths.append(("mirror_sessions_index", mirror._effective_sessions_index_path()))
        except Exception:
            pass
        assert_owner_runtime_paths(extra_paths)
    except Exception as exc:
        raise RuntimeError(f"owner worker startup self-check failed: {exc}") from exc

    @app.get("/internal/health")
    def health(_: None = Depends(_require_owner_token)) -> dict[str, Any]:
        from hermes_cli.owner_runtime import FORBIDDEN_OWNER_WORKER_ENV_KEYS, get_workspace_root

        return {
            "ready": True,
            "owner_key": owner_key,
            "owner_home": str(owner_home),
            "pid": os.getpid(),
            "hermes_home": str(get_hermes_home().resolve()),
            "workspace_root": str(get_workspace_root()),
            "control_home": str(app.state.owner_worker_control_home or ""),
            "forbidden_env_present": [key for key in FORBIDDEN_OWNER_WORKER_ENV_KEYS if os.environ.get(key, "").strip()],
        }

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
        db = _open_db()
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

    @app.get("/api/model/info")
    def get_model_info(profile: str | None = None, _: None = Depends(_require_owner_token)) -> dict[str, Any]:
        _reject_profile(profile)
        from hermes_cli.config import load_config
        from hermes_cli.model_info_payload import model_info_payload_from_config

        return model_info_payload_from_config(load_config())

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
        db = _open_db()
        try:
            return session_api.latest_descendant_payload(db, session_id)
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
    app = create_app(owner_key, owner_home)
    os.umask(0o077)
    uvicorn.run(app, uds=str(socket_path), log_level="warning", access_log=False)


if __name__ == "__main__":  # pragma: no cover
    main()
