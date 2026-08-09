"""Trusted Agent-bound tools for managed employee collaboration."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

_CREATE = "create_internal_group"
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
                    "invitee_account_ids": {
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
                    "first_round_target_account_ids": {
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
                    "invitee_account_ids",
                    "origin_attachment_ids",
                    "first_round_target_account_ids",
                    "idempotency_key",
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
                "Every round must name exact target account IDs; response text and @ "
                "references cannot schedule members."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "instruction": {"type": "string", "minLength": 1},
                    "target_account_ids": {
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
                    "target_account_ids",
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
            "invitee_account_ids",
            "origin_attachment_ids",
            "first_round_target_account_ids",
            "idempotency_key",
        }
    ),
    _DISPATCH: frozenset(
        {"instruction", "target_account_ids", "attachment_ids", "idempotency_key"}
    ),
    _FINISH: frozenset({"summary", "idempotency_key"}),
}


@dataclass(frozen=True)
class CollaborationAgentContext:
    """Server-created execution context that is never accepted from tool arguments."""

    service: Any
    creator_account_id: str
    source_kind: str
    source_conversation_id: str
    source_provider: str = "web"
    source_account_id: str | None = None
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


def tool_definitions(*, role: str, may_create: bool = False) -> list[dict[str, Any]]:
    """Return detached schemas for one trusted collaboration execution role."""
    if role == "source":
        allowed = {_CREATE} if may_create else set()
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
        if function_name == _CREATE:
            result = context.service.create_internal_group(
                context=context,
                title=_required_text(function_args["title"], "title"),
                brief=_required_text(function_args["brief"], "brief"),
                invitee_account_ids=_string_list(
                    function_args["invitee_account_ids"], "invitee account IDs", required=True
                ),
                origin_attachment_ids=_string_list(
                    function_args["origin_attachment_ids"], "origin attachment IDs"
                ),
                first_round_target_account_ids=_string_list(
                    function_args["first_round_target_account_ids"],
                    "first-round target account IDs",
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
                target_account_ids=_string_list(
                    function_args["target_account_ids"], "target account IDs", required=True
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
