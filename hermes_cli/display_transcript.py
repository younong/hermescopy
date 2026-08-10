"""Import-light conversion of persisted rows into display transcript messages."""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

ImageDimensions = Callable[[object], tuple[int, int] | None]
ToolContext = Callable[[str, dict[str, Any]], str]

_REASONING_KEYS = (
    "reasoning",
    "reasoning_content",
    "reasoning_details",
    "codex_reasoning_items",
)


def format_display_transcript(
    history: list[dict[str, Any]],
    *,
    image_dimensions: ImageDimensions | None = None,
    tool_context: ToolContext | None = None,
) -> list[dict[str, Any]]:
    """Convert persisted conversation rows to the shared display DTO."""
    messages: list[dict[str, Any]] = []
    tool_call_args: dict[str, tuple[str, dict[str, Any]]] = {}
    pending_image_artifacts: list[dict[str, Any]] = []

    for row in history:
        if not isinstance(row, dict):
            continue
        role = row.get("role")
        if role not in {"user", "assistant", "tool", "system"}:
            continue
        content_text = coerce_message_text(row.get("content"))
        display_card = row.get("_display_card")
        if isinstance(display_card, dict):
            message = {
                "role": "system",
                "text": str(display_card.get("text") or ""),
                "collaboration_card": dict(display_card),
            }
            card_id = row.get("_display_card_id")
            if card_id:
                message["id"] = str(card_id)
            if row.get("timestamp") is not None:
                message["timestamp"] = row["timestamp"]
            messages.append(message)
            continue
        if role == "assistant" and row.get("tool_calls"):
            for tool_call in row["tool_calls"]:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function", {})
                tool_call_id = tool_call.get("id", "")
                if not isinstance(function, dict) or not tool_call_id or not function.get("name"):
                    continue
                try:
                    args = json.loads(function.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    args = {}
                tool_call_args[str(tool_call_id)] = (
                    str(function["name"]),
                    args if isinstance(args, dict) else {},
                )
            if not content_text.strip():
                continue
        if role == "tool":
            tool_call_id = str(row.get("tool_call_id") or "")
            tool_info = tool_call_args.get(tool_call_id) if tool_call_id else None
            name = str(
                (tool_info[0] if tool_info else None)
                or row.get("_display_tool_name")
                or row.get("tool_name")
                or "tool"
            )
            args = (
                (tool_info[1] if tool_info else None)
                or row.get("_display_tool_args")
                or {}
            )
            if not isinstance(args, dict):
                args = {}
            if name in {"image_generate", "image_generation"}:
                try:
                    result = json.loads(row.get("content") or "")
                except (json.JSONDecodeError, TypeError):
                    result = None
                if isinstance(result, dict) and result.get("success") is not False:
                    source = next(
                        (
                            result.get(key)
                            for key in ("host_image", "image", "url")
                            if isinstance(result.get(key), str) and result.get(key).strip()
                        ),
                        None,
                    )
                    if source:
                        artifact: dict[str, Any] = {
                            "type": "artifact.image",
                            "url": source,
                            "title": "Generated image",
                            "tool_call_id": tool_call_id or None,
                        }
                        dimensions = positive_image_dimensions(result)
                        if dimensions is None and image_dimensions is not None:
                            dimensions = image_dimensions(source)
                        if dimensions is not None:
                            artifact["width"], artifact["height"] = dimensions
                        pending_image_artifacts.append(artifact)
                continue
            message = {
                "role": "tool",
                "name": name,
                "context": tool_context(name, args) if tool_context is not None else "",
            }
            if content_text:
                message["text"] = content_text
            if tool_call_id:
                message["tool_call_id"] = tool_call_id
            _add_stable_id(message, row)
            if row.get("timestamp") is not None:
                message["timestamp"] = row["timestamp"]
            messages.append(message)
            continue

        has_reasoning = role == "assistant" and any(row.get(key) for key in _REASONING_KEYS)
        attachments = valid_history_attachments(
            row.get("attachments"), image_dimensions=image_dimensions
        )
        image_artifacts = pending_image_artifacts if role == "assistant" else []
        if not content_text.strip() and not has_reasoning and not attachments and not image_artifacts:
            continue
        message = {"role": role, "text": content_text}
        if image_artifacts:
            message["content"] = list(image_artifacts)
            pending_image_artifacts.clear()
        _add_stable_id(message, row)
        if row.get("timestamp") is not None:
            message["timestamp"] = row["timestamp"]
        if attachments:
            message["attachments"] = attachments
        if role == "assistant":
            for key in _REASONING_KEYS:
                if key in row and row.get(key) is not None:
                    message[key] = row.get(key)
        messages.append(message)

    return messages


def coerce_message_text(content: Any) -> str:
    """Render provider string or multimodal message content as display text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (int, float)):
        return str(content)
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
                continue
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
                continue
            kind = part.get("type")
            if kind in {"text", "input_text", "output_text"}:
                text = part.get("text") or part.get("content") or ""
                if text:
                    chunks.append(str(text))
            elif kind in {"image_url", "input_image", "image"}:
                chunks.append(f"\n{_image_url(part) or '[image]'}")
            elif kind in {"input_audio", "audio"}:
                chunks.append("\n[audio]")
            elif kind:
                chunks.append(f"\n[{kind}]")
        return "".join(chunks)
    if isinstance(content, dict):
        kind = content.get("type")
        if kind in {"text", "input_text", "output_text"}:
            return str(content.get("text") or content.get("content") or "")
        if kind in {"image_url", "input_image", "image"}:
            return _image_url(content) or "[image]"
        if kind in {"input_audio", "audio"}:
            return "[audio]"
        if kind:
            return f"[{kind}]"
        if "text" in content:
            return str(content.get("text") or "")
        return "[structured content]"
    return str(content)


def valid_history_attachments(
    value: Any, *, image_dimensions: ImageDimensions | None = None
) -> list[dict[str, Any]]:
    """Return the persisted attachment subset safe to expose to clients."""
    if not isinstance(value, list):
        return []
    valid: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        name = item.get("name")
        if kind not in {"image", "pdf", "file"} or not isinstance(name, str) or not name:
            continue
        attachment = dict(item)
        dimensions = positive_image_dimensions(attachment)
        attachment.pop("width", None)
        attachment.pop("height", None)
        if kind == "image":
            if dimensions is None and image_dimensions is not None:
                dimensions = image_dimensions(attachment.get("path"))
            if dimensions is not None:
                attachment["width"], attachment["height"] = dimensions
        valid.append(attachment)
    return valid


def positive_image_dimensions(value: dict[str, Any]) -> tuple[int, int] | None:
    width = value.get("width")
    height = value.get("height")
    if (
        isinstance(width, int)
        and not isinstance(width, bool)
        and width > 0
        and isinstance(height, int)
        and not isinstance(height, bool)
        and height > 0
    ):
        return width, height
    return None


def _add_stable_id(message: dict[str, Any], row: dict[str, Any]) -> None:
    row_id = row.get("_row_id")
    if row_id is not None:
        message["id"] = f"db-{row.get('_session_id') or 'session'}-{row_id}"


def _image_url(content: dict[str, Any]) -> str:
    image_url = content.get("image_url")
    if isinstance(image_url, dict):
        candidate = image_url.get("url")
        return candidate if isinstance(candidate, str) else ""
    return image_url if isinstance(image_url, str) else ""
