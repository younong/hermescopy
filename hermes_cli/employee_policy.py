"""Normalization for immutable managed Feishu employee session policies."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping

_SCHEMA_VERSION = 1
_MAX_SYSTEM_PROMPT_CHARS = 200_000
_MAX_ITERATIONS = 500
_MAX_TOKENS = 1_000_000
_RELATIVE_COMPONENT = re.compile(r"^[^\x00/]+$")


class EmployeePolicyInvalid(ValueError):
    """A managed employee profile cannot be used as an execution policy."""


def _string_list(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise EmployeePolicyInvalid(f"{field} must be a list")
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text or text in result:
            raise EmployeePolicyInvalid(f"{field} contains an invalid entry")
        result.append(text)
    return tuple(result)


def _relative_path(value: Any, field: str, *, allow_empty: bool = False) -> str:
    path = str(value or "").strip()
    if not path and allow_empty:
        return ""
    if not path or path.startswith(("/", "~")) or "\\" in path:
        raise EmployeePolicyInvalid(f"{field} must be a relative path")
    components = path.split("/")
    if any(part in {"", ".", ".."} or not _RELATIVE_COMPONENT.fullmatch(part) for part in components):
        raise EmployeePolicyInvalid(f"{field} must be a controlled relative path")
    return "/".join(components)


def normalize_employee_source_policy(profile: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the source profile shape before it crosses the Worker boundary."""
    if not isinstance(profile, Mapping):
        raise EmployeePolicyInvalid("employee policy must be an object")
    required = {
        "schema_version",
        "model_registration_id",
        "system_prompt",
        "toolsets",
        "skills",
        "mcp_servers",
        "workspace_relative_path",
        "knowledge_relative_paths",
        "max_iterations",
    }
    optional = {"name", "role", "max_tokens"}
    unknown = set(profile) - required - optional
    missing = required - set(profile)
    if unknown or missing:
        raise EmployeePolicyInvalid("employee policy fields are invalid")
    if profile.get("schema_version") != _SCHEMA_VERSION:
        raise EmployeePolicyInvalid("employee policy schema version is invalid")
    model_registration_id = str(profile.get("model_registration_id") or "").strip()
    if not model_registration_id:
        raise EmployeePolicyInvalid("model_registration_id is required")
    system_prompt = profile.get("system_prompt")
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise EmployeePolicyInvalid("system_prompt is required")
    if len(system_prompt) > _MAX_SYSTEM_PROMPT_CHARS:
        raise EmployeePolicyInvalid("system_prompt is too large")
    toolsets = _string_list(profile.get("toolsets"), "toolsets")
    if any(item in {"all", "*"} for item in toolsets):
        raise EmployeePolicyInvalid("employee toolsets cannot use a wildcard")
    skills = _string_list(profile.get("skills"), "skills")
    mcp_servers = _string_list(profile.get("mcp_servers"), "mcp_servers")
    workspace = _relative_path(
        profile.get("workspace_relative_path"),
        "workspace_relative_path",
    )
    knowledge = tuple(
        _relative_path(item, "knowledge_relative_paths")
        for item in _string_list(profile.get("knowledge_relative_paths"), "knowledge_relative_paths")
    )
    workspace_parts = tuple(workspace.split("/"))
    for path in knowledge:
        knowledge_parts = tuple(path.split("/"))
        shared = min(len(workspace_parts), len(knowledge_parts))
        if workspace_parts[:shared] == knowledge_parts[:shared]:
            raise EmployeePolicyInvalid(
                "knowledge_relative_paths must not overlap the writable workspace"
            )
    try:
        max_iterations = int(profile.get("max_iterations"))
    except (TypeError, ValueError) as exc:
        raise EmployeePolicyInvalid("max_iterations must be an integer") from exc
    if isinstance(profile.get("max_iterations"), bool) or not 1 <= max_iterations <= _MAX_ITERATIONS:
        raise EmployeePolicyInvalid("max_iterations is outside the permitted bound")
    max_tokens = profile.get("max_tokens")
    if max_tokens is not None:
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError) as exc:
            raise EmployeePolicyInvalid("max_tokens must be an integer") from exc
        if isinstance(profile.get("max_tokens"), bool) or not 1 <= max_tokens <= _MAX_TOKENS:
            raise EmployeePolicyInvalid("max_tokens is outside the permitted bound")
    return {
        "schema_version": _SCHEMA_VERSION,
        "model_registration_id": model_registration_id,
        "system_prompt": system_prompt,
        "toolsets": list(toolsets),
        "skills": list(skills),
        "mcp_servers": list(mcp_servers),
        "workspace_relative_path": workspace,
        "knowledge_relative_paths": list(knowledge),
        "max_iterations": max_iterations,
        "max_tokens": max_tokens,
        **({"name": str(profile.get("name") or "").strip()} if "name" in profile else {}),
        **({"role": str(profile.get("role") or "").strip()} if "role" in profile else {}),
    }


def canonical_employee_snapshot(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """Return a JSON-stable snapshot and its non-secret policy fingerprint."""
    payload = json.dumps(dict(snapshot), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    normalized = json.loads(payload)
    fingerprint = "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
    normalized["snapshot_fingerprint"] = fingerprint
    return normalized, fingerprint
