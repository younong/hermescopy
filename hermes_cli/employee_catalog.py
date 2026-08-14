"""Owner-local catalog for managed AI employee policy editors."""
from __future__ import annotations

from pathlib import Path
from typing import Any


BUILTIN_ASSISTANT_SYSTEM_PROMPT = """You are AI Assistant, the Owner's built-in Hermes assistant. Help the Owner directly and use the available Hermes tools when they improve the result.

Before creating a managed employee, call list_employee_catalog and use only model registrations, skills, toolsets, and MCP servers from that live catalog.

When creating an internal collaboration group, explicitly provide the invitee employee IDs, a clear brief, and the first-round target employee IDs. Textual @mentions never select invitees or targets."""


def employee_catalog_payload(owner_home: Path) -> dict[str, Any]:
    from agent.models_dev import get_selectable_reasoning_levels
    from hermes_cli.config import load_config
    from hermes_cli.model_registrations import get_model_registrations_payload
    from hermes_cli.owner_runtime import owner_worker_runtime_paths
    from hermes_cli.skills_config import _list_all_skills, get_disabled_skills
    from hermes_cli.tools_config import enabled_mcp_server_names
    from toolsets import get_all_toolsets

    config = load_config()
    disabled_skills = get_disabled_skills(config, "feishu")
    registrations = get_model_registrations_payload()
    paths = owner_worker_runtime_paths(
        owner_home=owner_home.resolve(),
        worker_generation=1,
    )
    return {
        "model_registrations": [
            {
                **item,
                "reasoning_levels": list(get_selectable_reasoning_levels(
                    str(item.get("provider") or ""),
                    str(item.get("model") or ""),
                    allow_network=False,
                )),
            }
            for item in registrations["registrations"]
            if item.get("kind") == "chat"
        ],
        "active_chat": dict(registrations["active"]["chat"]),
        "toolsets": [
            {"name": name, "description": str(item.get("description") or "")}
            for name, item in sorted(get_all_toolsets().items())
            if name not in {"all", "*"} and not name.startswith("mcp-")
        ],
        "skills": [
            {
                "name": str(item.get("name") or ""),
                "description": str(item.get("description") or ""),
            }
            for item in _list_all_skills()
            if str(item.get("name") or "") not in disabled_skills
        ],
        "mcp_servers": sorted(enabled_mcp_server_names(config)),
        "workspace": {"root": "", "default": "default"},
        "knowledge_roots": [
            {"id": "default", "relative_path": "default"}
            for path in (paths.default_workspace,)
            if path.exists() and path.is_dir()
        ],
    }
