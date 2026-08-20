"""Scheduled Tasks dashboard backend routes.

Mounted at ``/api/plugins/scheduled-tasks`` in the Control Plane for local
multi-profile dashboards and in each authenticated Owner Worker for owner-local
execution.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from hermes_constants import get_hermes_home


MAX_OWNER_SCHEDULED_TASKS = 10


_OWNER_SELECTOR_KEYS = frozenset({"profile", "owner", "owner_home", "owner_key"})


def _reject_owner_selector_query(request: Request) -> None:
    if not getattr(request.app.state, "owner_worker_mode", False):
        return
    for key in _OWNER_SELECTOR_KEYS:
        values = request.query_params.getlist(key)
        if key == "profile":
            if any(str(value or "").strip().lower() not in {"", "default", "current"} for value in values):
                raise HTTPException(status_code=400, detail="profile selection is not available in authenticated mode")
        elif any(str(value or "").strip() for value in values):
            raise HTTPException(status_code=400, detail="owner selection is not available in authenticated mode")


router = APIRouter(dependencies=[Depends(_reject_owner_selector_query)])


class OwnerSelectors(BaseModel):
    profile: str | None = None
    owner: str | None = None
    owner_home: str | None = None
    owner_key: str | None = None
    model_config = {"extra": "forbid"}


class CronJobCreate(OwnerSelectors):
    prompt: str = ""
    schedule: str
    name: str = ""
    deliver: str = "local"
    skills: list[str] | None = None
    model: str | None = None
    provider: str | None = None
    base_url: str | None = None
    script: str | None = None
    context_from: Any = None
    enabled_toolsets: list[str] | None = None
    workdir: str | None = None
    no_agent: bool = False
    employee_id: str | None = None
    target_employee_ids: list[str] | None = None


class CronJobUpdate(OwnerSelectors):
    # Partial cron mutation payload. The cron management layer validates and
    # normalizes target_employee_ids alongside the other mutable fields.
    updates: dict[str, Any]


class AutomationBlueprintInstantiate(OwnerSelectors):
    blueprint: str
    values: dict[str, Any] = {}


def _owner_worker_mode(request: Request) -> bool:
    return bool(getattr(request.app.state, "owner_worker_mode", False))


def _reject_owner_selectors(*, profile: str | None = None, values: dict[str, Any] | None = None) -> None:
    values = values or {}
    if profile and str(profile).strip().lower() not in {"default", "current"}:
        raise HTTPException(status_code=400, detail="profile selection is not available in authenticated mode")
    if any(str(values.get(key) or "").strip() for key in ("owner", "owner_home", "owner_key")):
        raise HTTPException(status_code=400, detail="owner selection is not available in authenticated mode")


def _local_profile_home(profile: str | None) -> tuple[str, Path]:
    from hermes_cli.cron_dashboard import profile_home

    try:
        return profile_home(profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _home(request: Request, profile: str | None = None) -> tuple[str | None, Path]:
    if _owner_worker_mode(request):
        _reject_owner_selectors(profile=profile)
        return None, get_hermes_home()
    return _local_profile_home(profile)


def _profiles_for_lookup(request: Request) -> list[tuple[str | None, Path]]:
    if _owner_worker_mode(request):
        return [(None, get_hermes_home())]
    from hermes_cli.cron_dashboard import profile_homes

    return profile_homes()


def _find_job(request: Request, job_id: str, profile: str | None = None) -> tuple[str | None, Path]:
    from hermes_cli.cron_management import get_job

    if profile:
        name, home = _home(request, profile)
        if get_job(home, job_id):
            return name, home
        raise HTTPException(status_code=404, detail="Job not found")
    for name, home in _profiles_for_lookup(request):
        if get_job(home, job_id):
            return name, home
    raise HTTPException(status_code=404, detail="Job not found")


def _owner_workdir_root(request: Request) -> Path | None:
    if not _owner_worker_mode(request):
        return None
    return getattr(request.app.state, "owner_worker_workspace_root", None)


@router.get("/jobs")
def list_jobs_route(request: Request, profile: str = "all"):
    from hermes_cli.cron_management import list_jobs

    if _owner_worker_mode(request):
        if (profile or "all").strip().lower() not in {"", "all", "default", "current"}:
            _reject_owner_selectors(profile=profile)
        return list_jobs(get_hermes_home())
    if (profile or "all").strip().lower() != "all":
        name, home = _local_profile_home(profile)
        return list_jobs(home, profile=name)
    jobs: list[dict[str, Any]] = []
    for name, home in _profiles_for_lookup(request):
        jobs.extend(list_jobs(home, profile=name))
    return jobs


@router.get("/jobs/{job_id}")
def get_job_route(request: Request, job_id: str, profile: str | None = None):
    from hermes_cli.cron_management import get_job

    name, home = _find_job(request, job_id, profile)
    job = get_job(home, job_id, profile=name)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/jobs/{job_id}/runs")
def list_job_runs(request: Request, job_id: str, profile: str | None = None, limit: int = 20):
    from hermes_cli.cron_management import get_job
    from hermes_state import SessionDB

    name, home = _find_job(request, job_id, profile)
    job = get_job(home, job_id)
    canonical = str((job or {}).get("id") or job_id)
    limit_n = max(1, min(limit, 100))
    db = SessionDB(db_path=home / "state.db")
    try:
        runs = db.list_cron_job_runs(canonical, limit=limit_n, offset=0)
        now = time.time()
        for session in runs:
            session["is_active"] = session.get("ended_at") is None and (
                now - session.get("last_active", session.get("started_at", 0)) < 300
            )
            session["archived"] = bool(session.get("archived"))
            if name:
                session["profile"] = name
        return {"runs": runs, "limit": limit_n}
    finally:
        db.close()


@router.post("/jobs")
def create_job_route(request: Request, body: CronJobCreate, profile: str = "default"):
    from hermes_cli.cron_management import create_job

    values = body.model_dump(exclude={"profile", "owner", "owner_home", "owner_key"})
    if body.target_employee_ids:
        raise HTTPException(
            status_code=403,
            detail="target_employee_ids require a trusted employee scheduling context",
        )
    if _owner_worker_mode(request):
        selectors = body.model_dump(include={"owner", "owner_home", "owner_key"})
        _reject_owner_selectors(profile=body.profile or profile, values=selectors)
        home = get_hermes_home()
        try:
            return create_job(
                home,
                values,
                allowed_workdir_root=_owner_workdir_root(request),
                max_jobs=MAX_OWNER_SCHEDULED_TASKS,
            )
        except ValueError as exc:
            from cron.jobs import CronJobQuotaExceeded

            status_code = 409 if isinstance(exc, CronJobQuotaExceeded) else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    name, home = _local_profile_home(body.profile or profile)
    try:
        return create_job(home, values, profile=name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/jobs/{job_id}")
def update_job_route(request: Request, job_id: str, body: CronJobUpdate, profile: str | None = None):
    from hermes_cli.cron_management import update_job

    if _owner_worker_mode(request):
        selectors = body.model_dump(include={"owner", "owner_home", "owner_key"})
        _reject_owner_selectors(profile=body.profile or profile, values=selectors)
        forbidden = {"profile", "owner", "owner_home", "owner_key"}.intersection(body.updates)
        if "target_employee_ids" in body.updates:
            raise HTTPException(
                status_code=403,
                detail="target_employee_ids require a trusted employee scheduling context",
            )
        if forbidden:
            raise HTTPException(status_code=400, detail="owner and profile selection are not available in authenticated mode")
        name = None
        home = get_hermes_home()
    else:
        name, home = _find_job(request, job_id, body.profile or profile)
    try:
        job = update_job(
            home,
            job_id,
            body.updates,
            profile=name,
            allowed_workdir_root=_owner_workdir_root(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _mutate(request: Request, job_id: str, action: str, profile: str | None):
    from hermes_cli.cron_management import mutate_job

    name, home = _find_job(request, job_id, profile)
    job = mutate_job(home, job_id, action, profile=name)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/jobs/{job_id}/pause")
def pause_job(request: Request, job_id: str, profile: str | None = None):
    return _mutate(request, job_id, "pause", profile)


@router.post("/jobs/{job_id}/resume")
def resume_job(request: Request, job_id: str, profile: str | None = None):
    return _mutate(request, job_id, "resume", profile)


@router.post("/jobs/{job_id}/trigger")
def trigger_job(request: Request, job_id: str, profile: str | None = None):
    return _mutate(request, job_id, "trigger", profile)


@router.delete("/jobs/{job_id}")
def delete_job_route(request: Request, job_id: str, profile: str | None = None):
    from hermes_cli.cron_management import delete_job

    _name, home = _find_job(request, job_id, profile)
    try:
        removed = delete_job(home, job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"ok": True}


@router.get("/delivery-targets")
def delivery_targets_route(request: Request, profile: str = "default"):
    from hermes_cli.cron_management import delivery_targets

    _name, home = _home(request, profile)
    return {"targets": delivery_targets(home)}


@router.get("/blueprints")
def list_blueprints(request: Request, profile: str = "default"):
    from cron.blueprint_catalog import CATALOG, blueprint_catalog_entry
    from hermes_cli.cron_management import delivery_targets

    _name, home = _home(request, profile)
    options = ["origin", "local", *[target["id"] for target in delivery_targets(home) if target.get("id")]]
    entries = []
    for blueprint in CATALOG:
        entry = blueprint_catalog_entry(blueprint)
        for field in entry.get("fields", []):
            if field.get("name") == "deliver":
                field["options"] = options
        entries.append(entry)
    return {"blueprints": entries}


@router.post("/blueprints/instantiate")
def instantiate_blueprint(request: Request, body: AutomationBlueprintInstantiate, profile: str = "default"):
    from cron.blueprint_catalog import BlueprintFillError, fill_blueprint, get_blueprint
    from hermes_cli.cron_management import create_job

    if _owner_worker_mode(request):
        selectors = body.model_dump(include={"owner", "owner_home", "owner_key"})
        _reject_owner_selectors(profile=body.profile or profile, values=selectors)
        name = None
        home = get_hermes_home()
    else:
        name, home = _local_profile_home(body.profile or profile)
    blueprint = get_blueprint(body.blueprint)
    if blueprint is None:
        raise HTTPException(status_code=404, detail=f"Unknown blueprint: {body.blueprint}")
    try:
        spec = fill_blueprint(blueprint, body.values)
    except BlueprintFillError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    spec.pop("origin", None)
    try:
        return create_job(
            home,
            spec,
            profile=name,
            allowed_workdir_root=_owner_workdir_root(request),
            max_jobs=MAX_OWNER_SCHEDULED_TASKS if _owner_worker_mode(request) else None,
        )
    except ValueError as exc:
        from cron.jobs import CronJobQuotaExceeded

        status_code = 409 if isinstance(exc, CronJobQuotaExceeded) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
