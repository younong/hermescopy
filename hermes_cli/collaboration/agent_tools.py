"""Trusted Agent-bound tools for managed employee collaboration."""

from __future__ import annotations

from dataclasses import dataclass, fields
import json
from typing import Any

_CREATE = "create_internal_group"
_LIST_CATALOG = "list_employee_catalog"
_CREATE_EMPLOYEE = "create_managed_employee"
_DISPATCH = "dispatch_internal_group_round"
_FINISH = "finish_internal_group_task"

_TOOL_DEFINITIONS = (
    {
        "type": "function",
        "function": {
            "name": _CREATE,
            "description": (
                "Create one permanent internal collaboration group for this task. "
                "Invitees, first-round targets, the brief, and transferred attachments "
                "must all be explicit. Textual @ references never select targets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "minLength": 1},
                    "brief": {"type": "string", "minLength": 1},
                    "invitee_employee_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                    "origin_attachment_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "uniqueItems": True,
                    },
                    "first_round_target_employee_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                    "idempotency_key": {"type": "string", "minLength": 1},
                },
                "required": [
                    "title",
                    "brief",
                    "invitee_employee_ids",
                    "origin_attachment_ids",
                    "first_round_target_employee_ids",
                    "idempotency_key",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": _LIST_CATALOG,
            "description": (
                "List the Owner's live employee catalog, including current employees, "
                "active Chat model, and selectable models, skills, toolsets, and MCP servers."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": _CREATE_EMPLOYEE,
            "description": (
                "Create a managed employee from a complete policy validated against the live "
                "Owner catalog. Call list_employee_catalog immediately before using this tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "schema_version": {"type": "integer", "const": 1},
                    "name": {"type": "string", "minLength": 1},
                    "role": {"type": "string", "minLength": 1},
                    "model_registration_id": {"type": "string", "minLength": 1},
                    "system_prompt": {"type": "string", "minLength": 1},
                    "toolsets": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "uniqueItems": True,
                    },
                    "skills": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "uniqueItems": True,
                    },
                    "mcp_servers": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "uniqueItems": True,
                    },
                    "workspace_relative_path": {"type": "string", "minLength": 1},
                    "knowledge_relative_paths": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "uniqueItems": True,
                    },
                    "max_iterations": {"type": "integer", "minimum": 1},
                    "max_tokens": {"type": ["integer", "null"], "minimum": 1},
                },
                "required": [
                    "schema_version",
                    "name",
                    "role",
                    "model_registration_id",
                    "system_prompt",
                    "toolsets",
                    "skills",
                    "mcp_servers",
                    "workspace_relative_path",
                    "knowledge_relative_paths",
                    "max_iterations",
                    "max_tokens",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": _DISPATCH,
            "description": (
                "Dispatch the next explicit round for the internal collaboration task. "
                "Every round must name exact target employee IDs; response text and @ "
                "references cannot schedule members."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instruction": {"type": "string", "minLength": 1},
                    "target_employee_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                    "attachment_ids": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "uniqueItems": True,
                    },
                    "idempotency_key": {"type": "string", "minLength": 1},
                },
                "required": [
                    "instruction",
                    "target_employee_ids",
                    "attachment_ids",
                    "idempotency_key",
                ],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": _FINISH,
            "description": (
                "Finish the current internal collaboration task and post the final "
                "summary to its trusted web origin. Summary text never schedules targets."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "minLength": 1},
                    "idempotency_key": {"type": "string", "minLength": 1},
                },
                "required": ["summary", "idempotency_key"],
                "additionalProperties": False,
            },
        },
    },
)

_ALLOWED_ARGS = {
    _CREATE: frozenset(
        {
            "title",
            "brief",
            "invitee_employee_ids",
            "origin_attachment_ids",
            "first_round_target_employee_ids",
            "idempotency_key",
        }
    ),
    _LIST_CATALOG: frozenset(),
    _CREATE_EMPLOYEE: frozenset(
        {
            "schema_version",
            "name",
            "role",
            "model_registration_id",
            "system_prompt",
            "toolsets",
            "skills",
            "mcp_servers",
            "workspace_relative_path",
            "knowledge_relative_paths",
            "max_iterations",
            "max_tokens",
        }
    ),
    _DISPATCH: frozenset(
        {"instruction", "target_employee_ids", "attachment_ids", "idempotency_key"}
    ),
    _FINISH: frozenset({"summary", "idempotency_key"}),
}


@dataclass(frozen=True)
class CollaborationAgentContext:
    """Server-created execution context that is never accepted from tool arguments."""

    service: Any
    creator_employee_id: str
    source_kind: str
    source_conversation_id: str
    source_provider: str = "web"
    source_connector_account_id: str | None = None
    source_binding_id: str | None = None
    source_thread_id: str = ""
    source_session_id: str | None = None
    source_group_id: str | None = None
    source_event_id: str | None = None
    source_task_id: str | None = None
    source_depth: int = 0
    allowed_origin_attachment_ids: tuple[str, ...] = ()
    task_id: str | None = None
    role: str = "source"
    may_create_authorized: bool = False
    may_manage_employees: bool = False

    def same_agent_identity(self, other: object) -> bool:
        """Compare immutable identity and authority for one persistent Agent."""
        if not isinstance(other, CollaborationAgentContext):
            return False
        dynamic_fields = {"source_event_id", "allowed_origin_attachment_ids"}
        return all(
            getattr(self, field.name) == getattr(other, field.name)
            for field in fields(self)
            if field.name not in dynamic_fields
        )


def tool_definitions(
    *, role: str, may_create: bool = False, may_manage_employees: bool = False
) -> list[dict[str, Any]]:
    """Return detached schemas for one trusted collaboration execution role."""
    if role == "source":
        allowed = {_CREATE} if may_create else set()
        if may_manage_employees:
            allowed.update({_LIST_CATALOG, _CREATE_EMPLOYEE})
    elif role == "coordinator":
        allowed = {_DISPATCH, _FINISH}
    elif role == "member":
        allowed = set()
    else:
        raise ValueError("collaboration Agent role is invalid")
    return [
        json.loads(json.dumps(tool))
        for tool in _TOOL_DEFINITIONS
        if tool["function"]["name"] in allowed
    ]


def invoke(
    context: CollaborationAgentContext,
    function_name: str,
    function_args: dict[str, Any],
    *,
    tool_call_id: str | None,
) -> str:
    """Validate model arguments and execute against trusted service/context state."""
    if function_name not in _ALLOWED_ARGS:
        return _error("collaboration tool is unavailable")
    if not isinstance(function_args, dict) or set(function_args) != _ALLOWED_ARGS[function_name]:
        return _error("collaboration tool arguments are invalid")
    try:
        if function_name == _LIST_CATALOG:
            result = context.service.list_employee_catalog(context=context)
        elif function_name == _CREATE_EMPLOYEE:
            result = context.service.create_managed_employee(
                context=context,
                policy=dict(function_args),
            )
        elif function_name == _CREATE:
            result = context.service.create_internal_group(
                context=context,
                title=_required_text(function_args["title"], "title"),
                brief=_required_text(function_args["brief"], "brief"),
                invitee_employee_ids=_string_list(
                    function_args["invitee_employee_ids"], "invitee employee IDs", required=True
                ),
                origin_attachment_ids=_string_list(
                    function_args["origin_attachment_ids"], "origin attachment IDs"
                ),
                first_round_target_employee_ids=_string_list(
                    function_args["first_round_target_employee_ids"],
                    "first-round target employee IDs",
                    required=True,
                ),
                idempotency_key=_required_text(
                    function_args["idempotency_key"], "idempotency key"
                ),
                tool_call_id=str(tool_call_id or "").strip() or None,
            )
        elif function_name == _DISPATCH:
            result = context.service.dispatch_internal_group_round(
                context=context,
                instruction=_required_text(function_args["instruction"], "instruction"),
                target_employee_ids=_string_list(
                    function_args["target_employee_ids"], "target employee IDs", required=True
                ),
                attachment_ids=_string_list(
                    function_args["attachment_ids"], "attachment IDs"
                ),
                idempotency_key=_required_text(
                    function_args["idempotency_key"], "idempotency key"
                ),
                tool_call_id=str(tool_call_id or "").strip() or None,
            )
        else:
            result = context.service.finish_internal_group_task(
                context=context,
                summary=_required_text(function_args["summary"], "summary"),
                idempotency_key=_required_text(
                    function_args["idempotency_key"], "idempotency key"
                ),
                tool_call_id=str(tool_call_id or "").strip() or None,
            )
        return json.dumps({"success": True, **dict(result)}, ensure_ascii=False)
    except (TypeError, ValueError, RuntimeError) as exc:
        return _error(str(exc))


def is_collaboration_tool(name: str) -> bool:
    return name in _ALLOWED_ARGS


def _required_text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{label} is required")
    return text


def _string_list(value: Any, label: str, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    result = tuple(dict.fromkeys(_required_text(item, label) for item in value))
    if required and not result:
        raise ValueError(f"{label} are required")
    return result


def _error(message: str) -> str:
    return json.dumps({"success": False, "error": message}, ensure_ascii=False)
