"""Canonical model-request snapshots and final-payload context accounting.

A prepared request is the provider-shaped, request-middleware-adjusted payload that
Hermes will dispatch.  Compression, status surfaces, and request observability use
this same snapshot instead of independently reconstructing model input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from agent.model_metadata import estimate_messages_tokens_rough, estimate_tokens_rough


_CATEGORY_LABELS = {
    "system_prompt": "System prompt",
    "tool_definitions": "Tool definitions",
    "rules": "Rules",
    "skills": "Skills",
    "mcp": "MCP",
    "subagent_definitions": "Subagent definitions",
    "memory": "Memory",
    "conversation": "Conversation",
    "provider_overhead": "Provider overhead",
}
_CATEGORY_ORDER = tuple(_CATEGORY_LABELS)
_SKILLS_RE = re.compile(r"<available_skills>.*?</available_skills>", re.DOTALL)
_MEMORY_RE = re.compile(r"<memory-context>.*?</memory-context>", re.DOTALL)
_PROJECT_CONTEXT_RE = re.compile(r"# Project Context\b.*", re.DOTALL)


@dataclass(frozen=True)
class ModelRequestRoute:
    provider: str
    model: str
    base_url: str
    api_mode: str


@dataclass(frozen=True)
class RequestTokenCategory:
    id: str
    label: str
    tokens: int


@dataclass(frozen=True)
class PreparedRequestAccounting:
    raw_input_tokens: int
    effective_input_tokens: int
    output_token_limit: int
    context_limit: int
    compression_threshold: int
    hard_input_limit: int
    categories: Tuple[RequestTokenCategory, ...]
    source: str = "prepared_request"

    @property
    def requires_compression(self) -> bool:
        return self.effective_input_tokens >= self.compression_threshold

    @property
    def exceeds_hard_input_limit(self) -> bool:
        return self.effective_input_tokens >= self.hard_input_limit

    @property
    def context_percent(self) -> int:
        if self.context_limit <= 0:
            return 0
        return max(
            0,
            min(100, round(self.effective_input_tokens / self.context_limit * 100)),
        )


@dataclass(frozen=True)
class PreparedModelRequest:
    request_id: str
    route: ModelRequestRoute
    payload: Dict[str, Any]
    original_payload: Dict[str, Any]
    middleware_trace: Tuple[Dict[str, Any], ...]
    accounting: PreparedRequestAccounting
    message_count: int
    tool_count: int
    request_char_count: int
    dispatch_metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderManagedRequest:
    """Status marker for runtimes whose complete model input is provider-owned."""

    route: ModelRequestRoute
    accounting_source: str = "provider_managed"


def request_route(agent: Any) -> ModelRequestRoute:
    return ModelRequestRoute(
        provider=str(getattr(agent, "provider", "") or ""),
        model=str(getattr(agent, "model", "") or ""),
        base_url=str(getattr(agent, "base_url", "") or ""),
        api_mode=str(getattr(agent, "api_mode", "") or ""),
    )


def _value_tokens(value: Any) -> int:
    if value is None or value == "" or value == [] or value == {}:
        return 0
    if isinstance(value, str):
        return estimate_tokens_rough(value)
    return estimate_tokens_rough(str(value))


def _message_tokens(messages: Any) -> int:
    if not isinstance(messages, list):
        return _value_tokens(messages)
    if all(isinstance(item, dict) for item in messages):
        return estimate_messages_tokens_rough(messages)
    return _value_tokens(messages)


def _add(bucket: Dict[str, int], category: str, tokens: int) -> None:
    if tokens > 0:
        bucket[category] = bucket.get(category, 0) + int(tokens)


def _classify_text(text: str, *, default: str) -> Dict[str, int]:
    """Classify recognizable prompt blocks and reconcile to the whole string."""
    if not text:
        return {}

    matches: list[tuple[int, int, str, str]] = []
    for regex, category in (
        (_SKILLS_RE, "skills"),
        (_MEMORY_RE, "memory"),
        (_PROJECT_CONTEXT_RE, "rules"),
    ):
        for match in regex.finditer(text):
            if any(match.start() < end and match.end() > start for start, end, _, _ in matches):
                continue
            matches.append((match.start(), match.end(), category, match.group(0)))

    result: Dict[str, int] = {}
    claimed_tokens = 0
    for _, _, category, block in sorted(matches):
        tokens = estimate_tokens_rough(block)
        _add(result, category, tokens)
        claimed_tokens += tokens
    whole_tokens = estimate_tokens_rough(text)
    _add(result, default, max(0, whole_tokens - claimed_tokens))
    return result


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def _classify_messages(messages: Any, bucket: Dict[str, int]) -> None:
    if not isinstance(messages, list):
        _add(bucket, "conversation", _message_tokens(messages))
        return

    for message in messages:
        if not isinstance(message, dict):
            _add(bucket, "conversation", _value_tokens(message))
            continue
        role = str(message.get("role") or "")
        default = "system_prompt" if role in {"system", "developer"} else "conversation"
        total = estimate_messages_tokens_rough([message])
        text = _content_text(message.get("content"))
        classified = _classify_text(text, default=default)
        classified_total = sum(classified.values())
        for category, tokens in classified.items():
            _add(bucket, category, tokens)
        _add(bucket, default, max(0, total - classified_total))


def _tool_name(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ""
    function = tool.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "")
    spec = tool.get("toolSpec")
    if isinstance(spec, dict):
        return str(spec.get("name") or "")
    return str(tool.get("name") or "")


def _classify_tools(tools: Any, bucket: Dict[str, int]) -> None:
    if isinstance(tools, dict) and isinstance(tools.get("tools"), list):
        tools = tools["tools"]
    if not isinstance(tools, list):
        _add(bucket, "tool_definitions", _value_tokens(tools))
        return
    for tool in tools:
        name = _tool_name(tool)
        category = (
            "mcp"
            if name.startswith("mcp_") or name.startswith("mcp__")
            else "subagent_definitions"
            if name == "delegate_task"
            else "tool_definitions"
        )
        _add(bucket, category, _value_tokens(tool))


def _system_value(payload: Mapping[str, Any], mode: str) -> Any:
    if mode == "codex_responses":
        return payload.get("instructions")
    if mode == "anthropic_messages" or mode == "bedrock_converse":
        return payload.get("system")
    return None


def _messages_value(payload: Mapping[str, Any], mode: str) -> Any:
    if mode == "codex_responses":
        return payload.get("input")
    return payload.get("messages")


def _tools_value(payload: Mapping[str, Any], mode: str) -> Any:
    if mode == "bedrock_converse":
        return payload.get("toolConfig")
    return payload.get("tools")


def _output_token_limit(payload: Mapping[str, Any], fallback: int) -> int:
    for key in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
        value = payload.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    inference = payload.get("inferenceConfig")
    if isinstance(inference, dict):
        value = inference.get("maxTokens")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return max(0, int(fallback or 0))


def _scaled_categories(bucket: Dict[str, int], effective_total: int) -> Tuple[RequestTokenCategory, ...]:
    raw_total = sum(bucket.values())
    if raw_total <= 0:
        return ()
    scaled: Dict[str, int] = {}
    assigned = 0
    nonzero = [category for category in _CATEGORY_ORDER if bucket.get(category, 0) > 0]
    for category in nonzero:
        value = bucket[category] * effective_total // raw_total
        scaled[category] = value
        assigned += value
    if nonzero:
        scaled[nonzero[0]] += effective_total - assigned
    return tuple(
        RequestTokenCategory(category, _CATEGORY_LABELS[category], scaled[category])
        for category in _CATEGORY_ORDER
        if scaled.get(category, 0) > 0
    )


def account_prepared_payload(
    payload: Mapping[str, Any],
    *,
    api_mode: str,
    context_limit: int,
    compression_threshold: int,
    default_output_limit: int,
    calibrate: Optional[Any] = None,
) -> PreparedRequestAccounting:
    """Account for the final provider payload that will be dispatched."""
    mode = str(api_mode or "chat_completions")
    bucket: Dict[str, int] = {}

    system = _system_value(payload, mode)
    if system:
        text = _content_text(system) if isinstance(system, list) else str(system)
        classified = _classify_text(text, default="system_prompt")
        for category, tokens in classified.items():
            _add(bucket, category, tokens)
        _add(bucket, "system_prompt", max(0, _value_tokens(system) - sum(classified.values())))

    _classify_messages(_messages_value(payload, mode), bucket)
    _classify_tools(_tools_value(payload, mode), bucket)

    known = {
        "model", "modelId", "system", "instructions", "messages", "input", "tools", "toolConfig",
        "tool_choice", "parallel_tool_calls", "store", "stream", "include", "reasoning",
        "temperature", "top_p", "service_tier", "timeout", "extra_headers", "extra_body",
        "prompt_cache_key", "max_tokens", "max_completion_tokens", "max_output_tokens",
        "inferenceConfig", "guardrailConfig", "additionalModelRequestFields",
    }
    for key, value in payload.items():
        if key not in known:
            _add(bucket, "provider_overhead", _value_tokens({key: value}))

    raw_total = sum(bucket.values())
    effective_total = int(calibrate(raw_total) if calibrate is not None else raw_total)
    effective_total = max(raw_total, effective_total)
    output_limit = _output_token_limit(payload, default_output_limit)
    available = max(1, int(context_limit or 0) - output_limit)
    hard_input_limit = max(1, int(available * 0.95))
    return PreparedRequestAccounting(
        raw_input_tokens=raw_total,
        effective_input_tokens=effective_total,
        output_token_limit=output_limit,
        context_limit=max(0, int(context_limit or 0)),
        compression_threshold=max(0, int(compression_threshold or 0)),
        hard_input_limit=hard_input_limit,
        categories=_scaled_categories(bucket, effective_total),
    )


def prepare_model_request_snapshot(
    agent: Any,
    *,
    request_id: str,
    payload: Dict[str, Any],
    original_payload: Optional[Dict[str, Any]] = None,
    middleware_trace: Sequence[Dict[str, Any]] = (),
    dispatch_metadata: Optional[Mapping[str, Any]] = None,
) -> PreparedModelRequest:
    compressor = getattr(agent, "context_compressor", None)
    route = request_route(agent)
    calibrate = getattr(compressor, "calibrated_prompt_tokens", None)
    accounting = account_prepared_payload(
        payload,
        api_mode=route.api_mode,
        context_limit=int(getattr(compressor, "context_length", 0) or 0),
        compression_threshold=int(getattr(compressor, "threshold_tokens", 0) or 0),
        default_output_limit=int(getattr(agent, "max_tokens", 0) or 0),
        calibrate=calibrate,
    )
    messages = _messages_value(payload, route.api_mode)
    tools = _tools_value(payload, route.api_mode)
    if isinstance(tools, dict):
        tools = tools.get("tools")
    return PreparedModelRequest(
        request_id=request_id,
        route=route,
        payload=payload,
        original_payload=original_payload if original_payload is not None else payload,
        middleware_trace=tuple(middleware_trace),
        accounting=accounting,
        message_count=len(messages) if isinstance(messages, list) else 0,
        tool_count=len(tools) if isinstance(tools, list) else 0,
        request_char_count=len(str(payload)),
        dispatch_metadata=dict(dispatch_metadata or {}),
    )


def prepared_context_payload(prepared: Any) -> Optional[Dict[str, Any]]:
    """Return the stable first-party context payload for status surfaces."""
    if isinstance(prepared, ProviderManagedRequest):
        return {
            "categories": [],
            "context_max": 0,
            "context_percent": 0,
            "context_used": 0,
            "estimated_total": 0,
            "model": prepared.route.model,
            "accounting_source": prepared.accounting_source,
        }
    if not isinstance(prepared, PreparedModelRequest):
        return None
    accounting = prepared.accounting
    return {
        "categories": [
            {"id": item.id, "label": item.label, "tokens": item.tokens}
            for item in accounting.categories
        ],
        "context_max": accounting.context_limit,
        "context_percent": accounting.context_percent,
        "context_used": accounting.effective_input_tokens,
        "estimated_total": accounting.effective_input_tokens,
        "raw_estimated_total": accounting.raw_input_tokens,
        "model": prepared.route.model,
        "accounting_source": accounting.source,
    }
