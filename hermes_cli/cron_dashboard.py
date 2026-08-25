"""Local Dashboard profile resolution for scheduled-task management and fire."""
from __future__ import annotations

import re
from pathlib import Path

from hermes_constants import get_default_hermes_root, get_hermes_home


_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def normalize_profile_name(value: str | None) -> str:
    """Return a safe on-disk profile name accepted by Dashboard requests."""
    name = str(value or "default").strip().lower() or "default"
    if name != "default" and not _PROFILE_NAME_RE.fullmatch(name):
        raise ValueError(
            f"Invalid profile name {name!r}. Must match "
            "[a-z0-9][a-z0-9_-]{0,63}"
        )
    return name


def profile_home(value: str | None) -> tuple[str, Path]:
    """Resolve a local Dashboard profile to its Hermes home."""
    name = normalize_profile_name(value)
    root = get_default_hermes_root()
    home = root if name == "default" else root / "profiles" / name
    if name != "default" and not home.is_dir():
        raise FileNotFoundError(f"Profile {name!r} does not exist.")
    return name, home


def active_profile_name() -> str:
    """Infer the active profile name from the resolved Hermes home."""
    try:
        home = get_hermes_home().resolve()
        root = get_default_hermes_root().resolve()
        if home == root:
            return "default"
        relative = home.relative_to(root / "profiles")
        if len(relative.parts) == 1 and _PROFILE_NAME_RE.fullmatch(relative.parts[0]):
            return relative.parts[0]
    except (OSError, RuntimeError, ValueError):
        pass
    return "custom"


def profile_homes() -> list[tuple[str, Path]]:
    """Return the default local profile followed by valid named profiles."""
    root = get_default_hermes_root()
    result = [("default", root)]
    profiles_root = root / "profiles"
    if not profiles_root.is_dir():
        return result
    result.extend(
        (child.name, child)
        for child in sorted(profiles_root.iterdir())
        if child.is_dir() and _PROFILE_NAME_RE.fullmatch(child.name)
    )
    return result


def find_job_profile(job_id: str) -> str | None:
    """Return the first local profile containing the requested cron job."""
    from hermes_cli.cron_management import get_job

    for name, home in profile_homes():
        if get_job(home, job_id):
            return name
    return None
