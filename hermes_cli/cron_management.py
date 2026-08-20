"""Owner-scoped cron management operations used inside Owner Workers."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from cron.jobs import CronStore, use_store


def optional_text(value: Any, *, strip_trailing_slash: bool = False) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if strip_trailing_slash:
        text = text.rstrip("/")
    return text or None


def string_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        raw_items = re.split(r"[\n,]", value)
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        return None
    items = [str(item).strip() for item in raw_items if str(item).strip()]
    return items or None


def _store(home: str | Path) -> CronStore:
    return CronStore(home)


def _normalize_script(value: Any, home: Path) -> str | None:
    text = optional_text(value)
    if not text:
        return None

    scripts_root = (home / "scripts").resolve()
    raw_path = Path(text).expanduser()
    candidate = raw_path.resolve() if raw_path.is_absolute() else (scripts_root / raw_path).resolve()
    try:
        relative = candidate.relative_to(scripts_root)
    except ValueError as exc:
        raise ValueError(f"script must be inside {scripts_root}") from exc
    if not candidate.exists():
        raise ValueError(f"script does not exist: {candidate}")
    if not candidate.is_file():
        raise ValueError(f"script is not a file: {candidate}")
    return str(relative)


def _normalize_workdir(value: Any, allowed_root: Path | None) -> str | None:
    text = optional_text(value)
    if not text or allowed_root is None:
        return text
    candidate = Path(text).expanduser().resolve()
    root = allowed_root.expanduser().resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"workdir must be inside {root}") from exc
    if not candidate.is_dir():
        raise ValueError(f"workdir is not a directory: {candidate}")
    return str(candidate)


def _normalize_updates(
    updates: Mapping[str, Any],
    home: Path,
    *,
    allowed_workdir_root: Path | None,
) -> dict[str, Any]:
    normalized = dict(updates or {})
    for key in ("model", "provider", "employee_id"):
        if key in normalized:
            normalized[key] = optional_text(normalized[key])
    if "workdir" in normalized:
        normalized["workdir"] = _normalize_workdir(
            normalized["workdir"], allowed_workdir_root
        )
    if "script" in normalized:
        normalized["script"] = _normalize_script(normalized["script"], home)
    if "base_url" in normalized:
        normalized["base_url"] = optional_text(
            normalized["base_url"], strip_trailing_slash=True
        )
    if "deliver" in normalized:
        normalized["deliver"] = optional_text(normalized["deliver"]) or "local"
    if "context_from" in normalized:
        normalized["context_from"] = string_list(normalized["context_from"])
    if "enabled_toolsets" in normalized:
        normalized["enabled_toolsets"] = string_list(normalized["enabled_toolsets"])
    if "skills" in normalized:
        normalized["skills"] = string_list(normalized["skills"])
    return normalized


def _validate_effective_job(job: Mapping[str, Any]) -> None:
    prompt = optional_text(job.get("prompt"))
    script = optional_text(job.get("script"))
    skills = string_list(job.get("skills")) or string_list(job.get("skill"))
    no_agent = bool(job.get("no_agent"))

    if no_agent and not script:
        raise ValueError("no_agent=True requires a script")
    if not no_agent and not (prompt or skills or script):
        raise ValueError("agent cron jobs require a prompt, skill, or script")

    if optional_text(job.get("employee_id")):
        # An employee job runs under the employee's policy (model registration,
        # skills, toolsets, workspace) resolved at fire time, so the manual
        # execution knobs are mutually exclusive with it.
        conflicts = [
            label
            for label, value in (
                ("model", optional_text(job.get("model"))),
                ("provider", optional_text(job.get("provider"))),
                ("base_url", optional_text(job.get("base_url"))),
                ("skills", skills),
                ("enabled_toolsets", string_list(job.get("enabled_toolsets"))),
                ("script", script),
                ("workdir", optional_text(job.get("workdir"))),
            )
            if value
        ]
        if no_agent:
            conflicts.append("no_agent")
        if conflicts:
            raise ValueError(
                "employee cron jobs cannot set: " + ", ".join(conflicts)
            )

    if prompt:
        from tools.cronjob_tools import _scan_cron_prompt

        scan_error = _scan_cron_prompt(prompt)
        if scan_error:
            raise ValueError(scan_error)

    if skills and not no_agent:
        from tools.cronjob_tools import _scan_cron_skill_assembled
        from tools.skill_manager_tool import _find_skill

        for skill_name in skills:
            skill = _find_skill(skill_name)
            if not skill:
                raise ValueError(f"skill '{skill_name}' not found in this workspace")
            skill_md = Path(skill["path"]) / "SKILL.md"
            assembled = skill_md.read_text(encoding="utf-8")
            _scan_result, scan_error = _scan_cron_skill_assembled(assembled)
            if scan_error:
                raise ValueError(scan_error)

    if not no_agent:
        from tools.cronjob_tools import _validate_cron_base_url

        base_url_error = _validate_cron_base_url(
            job.get("provider"), job.get("base_url")
        )
        if base_url_error:
            raise ValueError(base_url_error)


def _validate_context_refs(refs: list[str] | None) -> None:
    if not refs:
        return
    from cron.jobs import get_job

    for ref in refs:
        if not get_job(ref):
            raise ValueError(f"context_from job '{ref}' not found in this workspace")


def _notify_provider() -> None:
    from cron.scheduler import _notify_provider_jobs_changed

    _notify_provider_jobs_changed()


def _annotate_job(
    job: Mapping[str, Any],
    *,
    profile: str | None,
    home: Path,
) -> dict[str, Any]:
    result = dict(job)
    if profile is not None:
        result.update(
            profile=profile,
            profile_name=profile,
            hermes_home=str(home),
            is_default_profile=profile == "default",
        )
    return result


def list_jobs(
    home: str | Path,
    *,
    profile: str | None = None,
    include_disabled: bool = True,
) -> list[dict[str, Any]]:
    store = _store(home)
    with use_store(store):
        from cron.jobs import list_jobs as load_jobs

        jobs = load_jobs(include_disabled)
    return [_annotate_job(job, profile=profile, home=store.owner_home) for job in jobs]


def get_job(
    home: str | Path,
    job_id: str,
    *,
    profile: str | None = None,
) -> dict[str, Any] | None:
    store = _store(home)
    with use_store(store):
        from cron.jobs import get_job as load_job

        job = load_job(job_id)
    return _annotate_job(job, profile=profile, home=store.owner_home) if job else None


def create_job(
    home: str | Path,
    values: Mapping[str, Any],
    *,
    profile: str | None = None,
    allowed_workdir_root: Path | None = None,
    max_jobs: int | None = None,
) -> dict[str, Any]:
    store = _store(home)
    normalized = _normalize_updates(
        values, store.owner_home, allowed_workdir_root=allowed_workdir_root
    )
    normalized.setdefault("prompt", "")
    normalized.setdefault("deliver", "local")
    _validate_effective_job(normalized)

    with use_store(store):
        _validate_context_refs(normalized.get("context_from"))
        from cron.jobs import create_job as persist_job

        job = persist_job(
            prompt=normalized.get("prompt") or "",
            schedule=normalized["schedule"],
            name=normalized.get("name") or "",
            deliver=normalized.get("deliver") or "local",
            skills=normalized.get("skills"),
            model=normalized.get("model"),
            provider=normalized.get("provider"),
            base_url=normalized.get("base_url"),
            script=normalized.get("script"),
            context_from=normalized.get("context_from"),
            enabled_toolsets=normalized.get("enabled_toolsets"),
            workdir=normalized.get("workdir"),
            no_agent=bool(normalized.get("no_agent")),
            employee_id=normalized.get("employee_id"),
            max_jobs=max_jobs,
        )
        _notify_provider()
    return _annotate_job(job, profile=profile, home=store.owner_home)


def update_job(
    home: str | Path,
    job_id: str,
    updates: Mapping[str, Any],
    *,
    profile: str | None = None,
    allowed_workdir_root: Path | None = None,
) -> dict[str, Any] | None:
    store = _store(home)
    normalized = _normalize_updates(
        updates, store.owner_home, allowed_workdir_root=allowed_workdir_root
    )
    with use_store(store):
        from cron.jobs import get_job as load_job
        from cron.jobs import update_job as persist_update

        existing = load_job(job_id)
        if not existing:
            return None
        effective = {**existing, **normalized}
        if "skills" in normalized and "skill" not in normalized:
            effective["skill"] = None
        _validate_effective_job(effective)
        if "context_from" in normalized:
            _validate_context_refs(normalized.get("context_from"))
        job = persist_update(job_id, normalized)
        if job:
            _notify_provider()
    return _annotate_job(job, profile=profile, home=store.owner_home) if job else None


def mutate_job(
    home: str | Path,
    job_id: str,
    action: str,
    *,
    profile: str | None = None,
) -> dict[str, Any] | None:
    store = _store(home)
    functions = {
        "pause": "pause_job",
        "resume": "resume_job",
        "trigger": "trigger_job",
    }
    if action not in functions:
        raise ValueError(f"unsupported cron action: {action}")
    with use_store(store):
        from cron import jobs as cron_jobs

        job = getattr(cron_jobs, functions[action])(job_id)
        if job:
            _notify_provider()
    return _annotate_job(job, profile=profile, home=store.owner_home) if job else None


def delete_job(home: str | Path, job_id: str) -> bool:
    with use_store(_store(home)):
        from cron.jobs import remove_job

        removed = remove_job(job_id)
        if removed:
            _notify_provider()
        return removed


def delivery_targets(home: str | Path) -> list[dict[str, Any]]:
    """Return Local plus platforms configured inside the selected owner home."""
    from gateway.config import load_gateway_config
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    selected_home = Path(home).expanduser().resolve()
    token = set_hermes_home_override(selected_home)
    try:
        config = load_gateway_config()
    finally:
        reset_hermes_home_override(token)

    targets = [{
        "id": "local",
        "name": "Local (save only)",
        "home_target_set": True,
        "home_env_var": None,
    }]
    for platform in config.get_connected_platforms():
        platform_id = str(getattr(platform, "value", platform))
        targets.append({
            "id": platform_id,
            "name": platform_id.replace("_", " ").title(),
            "home_target_set": config.get_home_channel(platform) is not None,
            "home_env_var": None,
        })
    return targets
