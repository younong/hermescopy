"""Message and tool-payload sanitization helpers.

Pure functions extracted from ``run_agent.py`` so the AIAgent module can
stay focused on the conversation loop.  These walk OpenAI-format message
lists and structured payloads, repairing or stripping problematic
characters that would otherwise crash ``json.dumps`` inside the OpenAI
SDK or be rejected by upstream APIs.

All helpers are stateless and side-effect-free except for in-place
mutation of their input (where documented).  Backward-compatible
re-exports from ``run_agent`` remain in place so existing imports
``from run_agent import _sanitize_surrogates`` keep working.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from agent.message_content import flatten_message_text

logger = logging.getLogger(__name__)

_CONTENT_JSON_PREFIX = "\x00json:"
_ATTACHMENT_PART_TYPES = frozenset(
    {
        "audio",
        "document",
        "file",
        "image",
        "image_url",
        "input_audio",
        "input_file",
        "input_image",
        "resource",
        "resource_link",
    }
)
_ATTACHMENT_KIND_BY_TYPE = {
    "audio": "audio",
    "document": "document",
    "file": "file",
    "image": "image",
    "image_url": "image",
    "input_audio": "audio",
    "input_file": "file",
    "input_image": "image",
    "resource": "file",
    "resource_link": "file",
}

# Lone surrogate code points are invalid in UTF-8 and crash json.dumps
# inside the OpenAI SDK.  Used by every surrogate-sanitization helper
# below as well as by run_agent and the CLI for paste-from-clipboard
# scrubbing.
_SURROGATE_RE = re.compile(r'[\ud800-\udfff]')


def _sanitize_surrogates(text: str) -> str:
    """Replace lone surrogate code points with U+FFFD (replacement character).

    Surrogates are invalid in UTF-8 and will crash ``json.dumps()`` inside the
    OpenAI SDK.  This is a fast no-op when the text contains no surrogates.
    """
    if _SURROGATE_RE.search(text):
        return _SURROGATE_RE.sub('\ufffd', text)
    return text


def _sanitize_structure_surrogates(payload: Any) -> bool:
    """Replace surrogate code points in nested dict/list payloads in-place.

    Mirror of ``_sanitize_structure_non_ascii`` but for surrogate recovery.
    Used to scrub nested structured fields (e.g. ``reasoning_details`` — an
    array of dicts with ``summary``/``text`` strings) that flat per-field
    checks don't reach.  Returns True if any surrogates were replaced.
    """
    found = False

    def _walk(node):
        nonlocal found
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str):
                    if _SURROGATE_RE.search(value):
                        node[key] = _SURROGATE_RE.sub('\ufffd', value)
                        found = True
                elif isinstance(value, (dict, list)):
                    _walk(value)
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                if isinstance(value, str):
                    if _SURROGATE_RE.search(value):
                        node[idx] = _SURROGATE_RE.sub('\ufffd', value)
                        found = True
                elif isinstance(value, (dict, list)):
                    _walk(value)

    _walk(payload)
    return found


def _sanitize_messages_surrogates(messages: list) -> bool:
    """Sanitize surrogate characters from all string content in a messages list.

    Walks message dicts in-place. Returns True if any surrogates were found
    and replaced, False otherwise. Covers content/text, name, tool call
    metadata/arguments, AND any additional string or nested structured fields
    (``reasoning``, ``reasoning_content``, ``reasoning_details``, etc.) so
    retries don't fail on a non-content field.  Byte-level reasoning models
    (xiaomi/mimo, kimi, glm) can emit lone surrogates in reasoning output
    that flow through to ``api_messages["reasoning_content"]`` on the next
    turn and crash json.dumps inside the OpenAI SDK.
    """
    found = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, str) and _SURROGATE_RE.search(content):
            msg["content"] = _SURROGATE_RE.sub('\ufffd', content)
            found = True
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str) and _SURROGATE_RE.search(text):
                        part["text"] = _SURROGATE_RE.sub('\ufffd', text)
                        found = True
        name = msg.get("name")
        if isinstance(name, str) and _SURROGATE_RE.search(name):
            msg["name"] = _SURROGATE_RE.sub('\ufffd', name)
            found = True
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                tc_id = tc.get("id")
                if isinstance(tc_id, str) and _SURROGATE_RE.search(tc_id):
                    tc["id"] = _SURROGATE_RE.sub('\ufffd', tc_id)
                    found = True
                fn = tc.get("function")
                if isinstance(fn, dict):
                    fn_name = fn.get("name")
                    if isinstance(fn_name, str) and _SURROGATE_RE.search(fn_name):
                        fn["name"] = _SURROGATE_RE.sub('\ufffd', fn_name)
                        found = True
                    fn_args = fn.get("arguments")
                    if isinstance(fn_args, str) and _SURROGATE_RE.search(fn_args):
                        fn["arguments"] = _SURROGATE_RE.sub('\ufffd', fn_args)
                        found = True
        # Walk any additional string / nested fields (reasoning,
        # reasoning_content, reasoning_details, etc.) — surrogates from
        # byte-level reasoning models (xiaomi/mimo, kimi, glm) can lurk
        # in these fields and aren't covered by the per-field checks above.
        # Matches _sanitize_messages_non_ascii's coverage (PR #10537).
        for key, value in msg.items():
            if key in {"content", "name", "tool_calls", "role"}:
                continue
            if isinstance(value, str):
                if _SURROGATE_RE.search(value):
                    msg[key] = _SURROGATE_RE.sub('\ufffd', value)
                    found = True
            elif isinstance(value, (dict, list)):
                if _sanitize_structure_surrogates(value):
                    found = True
    return found


def _escape_invalid_chars_in_json_strings(raw: str) -> str:
    """Escape unescaped control chars inside JSON string values.

    Walks the raw JSON character-by-character, tracking whether we are
    inside a double-quoted string. Inside strings, replaces literal
    control characters (0x00-0x1F) that aren't already part of an escape
    sequence with their ``\\uXXXX`` equivalents. Pass-through for everything
    else.

    Ported from #12093 — complements the other repair passes in
    ``_repair_tool_call_arguments`` when ``json.loads(strict=False)`` is
    not enough (e.g. llama.cpp backends that emit literal apostrophes or
    tabs alongside other malformations).
    """
    out: list[str] = []
    in_string = False
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if in_string:
            if ch == "\\" and i + 1 < n:
                # Already-escaped char — pass through as-is
                out.append(ch)
                out.append(raw[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
                out.append(ch)
            elif ord(ch) < 0x20:
                out.append(f"\\u{ord(ch):04x}")
            else:
                out.append(ch)
        else:
            if ch == '"':
                in_string = True
            out.append(ch)
        i += 1
    return "".join(out)


def _repair_tool_call_arguments(raw_args: str, tool_name: str = "?") -> str:
    """Attempt to repair malformed tool_call argument JSON.

    Models like GLM-5.1 via Ollama can produce truncated JSON, trailing
    commas, Python ``None``, etc.  The API proxy rejects these with HTTP 400
    "invalid tool call arguments".  This function applies common repairs;
    if all fail it returns ``"{}"`` so the request succeeds (better than
    crashing the session).  All repairs are logged at WARNING level.
    """
    raw_stripped = raw_args.strip() if isinstance(raw_args, str) else ""

    # Fast-path: empty / whitespace-only -> empty object
    if not raw_stripped:
        logger.warning("Sanitized empty tool_call arguments for %s", tool_name)
        return "{}"

    # Python-literal None -> normalise to {}
    if raw_stripped == "None":
        logger.warning("Sanitized Python-None tool_call arguments for %s", tool_name)
        return "{}"

    # Repair pass 0: llama.cpp backends sometimes emit literal control
    # characters (tabs, newlines) inside JSON string values. json.loads
    # with strict=False accepts these and lets us re-serialise the
    # result into wire-valid JSON without any string surgery. This is
    # the most common local-model repair case (#12068).
    try:
        parsed = json.loads(raw_stripped, strict=False)
        reserialised = json.dumps(parsed, separators=(",", ":"))
        if reserialised != raw_stripped:
            logger.warning(
                "Repaired unescaped control chars in tool_call arguments for %s",
                tool_name,
            )
        return reserialised
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Attempt common JSON repairs
    fixed = raw_stripped
    # 1. Strip trailing commas before } or ]
    fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
    # 2. Close unclosed structures
    open_curly = fixed.count('{') - fixed.count('}')
    open_bracket = fixed.count('[') - fixed.count(']')
    if open_curly > 0:
        fixed += '}' * open_curly
    if open_bracket > 0:
        fixed += ']' * open_bracket
    # 3. Remove excess closing braces/brackets (bounded to 50 iterations)
    for _ in range(50):
        try:
            json.loads(fixed)
            break
        except json.JSONDecodeError:
            if fixed.endswith('}') and fixed.count('}') > fixed.count('{'):
                fixed = fixed[:-1]
            elif fixed.endswith(']') and fixed.count(']') > fixed.count('['):
                fixed = fixed[:-1]
            else:
                break

    try:
        json.loads(fixed)
        logger.warning(
            "Repaired malformed tool_call arguments for %s: %s → %s",
            tool_name, raw_stripped[:80], fixed[:80],
        )
        return fixed
    except json.JSONDecodeError:
        pass

    # Repair pass 4: escape unescaped control chars inside JSON strings,
    # then retry. Catches cases where strict=False alone fails because
    # other malformations are present too.
    try:
        escaped = _escape_invalid_chars_in_json_strings(fixed)
        if escaped != fixed:
            json.loads(escaped)
            logger.warning(
                "Repaired control-char-laced tool_call arguments for %s: %s → %s",
                tool_name, raw_stripped[:80], escaped[:80],
            )
            return escaped
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Last resort: replace with empty object so the API request doesn't
    # crash the entire session.
    logger.warning(
        "Unrepairable tool_call arguments for %s — "
        "replaced with empty object (was: %s)",
        tool_name, raw_stripped[:80],
    )
    return "{}"


def close_interrupted_tool_sequence(messages: list, final_response: Any = None) -> bool:
    """Append a synthetic assistant turn when an interrupted tail is a tool result.

    A turn cut short by ``/stop`` can leave the transcript ending on a raw
    ``tool`` message (a tool finished, or its execution was cancelled, but the
    model never streamed a closing assistant turn). Persisting that tail means
    the next user message lands as ``… tool → user`` — a role-alternation
    violation that strict providers (Gemini, Claude) react to by hallucinating
    a continuation of the user's message and ignoring prior context, which
    reads to the user as "lost context" (#48879).

    ``finalize_turn`` closes this on the happy interrupt path, but the
    retry/backoff/error interrupt aborts in ``conversation_loop`` ``return``
    early and never reach it — this shared helper closes the sequence on all of
    them. ``final_response`` is usually empty on an interrupt, so an explicit
    placeholder is used rather than an empty-content assistant turn.

    Mutates ``messages`` in place. Returns True if a closing turn was appended.
    """
    if not messages:
        return False
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "tool":
        return False
    text = final_response if isinstance(final_response, str) else ""
    messages.append({
        "role": "assistant",
        "content": text.strip() or "Operation interrupted.",
    })
    return True


def _strip_non_ascii(text: str) -> str:
    """Remove non-ASCII characters, replacing with closest ASCII equivalent or removing.

    Used as a last resort when the system encoding is ASCII and can't handle
    any non-ASCII characters (e.g. LANG=C on Chromebooks).
    """
    return text.encode('ascii', errors='ignore').decode('ascii')


def _sanitize_messages_non_ascii(messages: list) -> bool:
    """Strip non-ASCII characters from all string content in a messages list.

    This is a last-resort recovery for systems with ASCII-only encoding
    (LANG=C, Chromebooks, minimal containers).  Returns True if any
    non-ASCII content was found and sanitized.
    """
    found = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        # Sanitize content (string)
        content = msg.get("content")
        if isinstance(content, str):
            sanitized = _strip_non_ascii(content)
            if sanitized != content:
                msg["content"] = sanitized
                found = True
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        sanitized = _strip_non_ascii(text)
                        if sanitized != text:
                            part["text"] = sanitized
                            found = True
        # Sanitize name field (can contain non-ASCII in tool results)
        name = msg.get("name")
        if isinstance(name, str):
            sanitized = _strip_non_ascii(name)
            if sanitized != name:
                msg["name"] = sanitized
                found = True
        # Sanitize tool_calls
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    if isinstance(fn, dict):
                        fn_args = fn.get("arguments")
                        if isinstance(fn_args, str):
                            sanitized = _strip_non_ascii(fn_args)
                            if sanitized != fn_args:
                                fn["arguments"] = sanitized
                                found = True
        # Sanitize any additional top-level string fields (e.g. reasoning_content)
        for key, value in msg.items():
            if key in {"content", "name", "tool_calls", "role"}:
                continue
            if isinstance(value, str):
                sanitized = _strip_non_ascii(value)
                if sanitized != value:
                    msg[key] = sanitized
                    found = True
    return found


def _sanitize_tools_non_ascii(tools: list) -> bool:
    """Strip non-ASCII characters from tool payloads in-place."""
    return _sanitize_structure_non_ascii(tools)


def _decode_structured_content(content: Any) -> tuple[Any, bool]:
    """Decode legacy SessionDB structured content without mutating its source."""
    if not isinstance(content, str) or not content.startswith(_CONTENT_JSON_PREFIX):
        return content, False
    try:
        decoded = json.loads(content[len(_CONTENT_JSON_PREFIX):])
    except (TypeError, ValueError):
        return content, False
    return decoded, True


def _is_attachment_part(part: Any) -> bool:
    """Return whether a rich content part carries or references an attachment."""
    return isinstance(part, dict) and part.get("type") in _ATTACHMENT_PART_TYPES


def summarize_tool_result(tool_name: str, tool_args: str, tool_content: str) -> str:
    """Create an informative one-line summary of a tool call and result."""
    try:
        args = json.loads(tool_args) if tool_args else {}
    except (json.JSONDecodeError, TypeError):
        args = {}
    if not isinstance(args, dict):
        args = {}

    content = tool_content or ""
    content_len = len(content)
    line_count = content.count("\n") + 1 if content.strip() else 0

    if tool_name == "terminal":
        cmd = str(args.get("command", ""))
        if len(cmd) > 80:
            cmd = cmd[:77] + "..."
        exit_match = re.search(r'"exit_code"\s*:\s*(-?\d+)', content)
        exit_code = exit_match.group(1) if exit_match else "?"
        return f"[terminal] ran `{cmd}` -> exit {exit_code}, {line_count} lines output"

    if tool_name == "read_file":
        path = args.get("path", "?")
        offset = args.get("offset", 1)
        return f"[read_file] read {path} from line {offset} ({content_len:,} chars)"

    if tool_name == "write_file":
        path = args.get("path", "?")
        written = args.get("content", "")
        written_lines = written.count("\n") + 1 if isinstance(written, str) and written else "?"
        return f"[write_file] wrote to {path} ({written_lines} lines)"

    if tool_name == "search_files":
        pattern = args.get("pattern", "?")
        path = args.get("path", ".")
        target = args.get("target", "content")
        match_count = re.search(r'"total_count"\s*:\s*(\d+)', content)
        count = match_count.group(1) if match_count else "?"
        return f"[search_files] {target} search for '{pattern}' in {path} -> {count} matches"

    if tool_name == "patch":
        path = args.get("path", "?")
        mode = args.get("mode", "replace")
        return f"[patch] {mode} in {path} ({content_len:,} chars result)"

    if tool_name in {
        "browser_navigate",
        "browser_click",
        "browser_snapshot",
        "browser_type",
        "browser_scroll",
        "browser_vision",
    }:
        url = args.get("url", "")
        ref = args.get("ref", "")
        detail = f" {url}" if url else (f" ref={ref}" if ref else "")
        return f"[{tool_name}]{detail} ({content_len:,} chars)"

    if tool_name == "web_search":
        query = args.get("query", "?")
        return f"[web_search] query='{query}' ({content_len:,} chars result)"

    if tool_name == "web_extract":
        urls = args.get("urls", [])
        url_desc = urls[0] if isinstance(urls, list) and urls else "?"
        if isinstance(urls, list) and len(urls) > 1:
            url_desc = f"{url_desc} (+{len(urls) - 1} more)"
        return f"[web_extract] {url_desc} ({content_len:,} chars)"

    if tool_name == "delegate_task":
        goal = str(args.get("goal", ""))
        if len(goal) > 60:
            goal = goal[:57] + "..."
        return f"[delegate_task] '{goal}' ({content_len:,} chars result)"

    if tool_name == "execute_code":
        code = str(args.get("code") or "")
        code_preview = code[:60].replace("\n", " ")
        if len(code) > 60:
            code_preview += "..."
        return f"[execute_code] `{code_preview}` ({line_count} lines output)"

    if tool_name in {"skill_view", "skills_list", "skill_manage"}:
        name = args.get("name", "?")
        return f"[{tool_name}] name={name} ({content_len:,} chars)"

    if tool_name == "vision_analyze":
        question = str(args.get("question", ""))[:50]
        return f"[vision_analyze] '{question}' ({content_len:,} chars)"

    if tool_name == "memory":
        action = args.get("action", "?")
        target = args.get("target", "?")
        return f"[memory] {action} on {target}"
    if tool_name == "todo":
        return "[todo] updated task list"
    if tool_name == "clarify":
        return "[clarify] asked user a question"
    if tool_name == "text_to_speech":
        return f"[text_to_speech] generated audio ({content_len:,} chars)"
    if tool_name == "cronjob":
        return f"[cronjob] {args.get('action', '?')}"
    if tool_name == "process":
        return f"[process] {args.get('action', '?')} session={args.get('session_id', '?')}"

    first_args = "".join(
        f" {key}={str(value)[:40]}" for key, value in list(args.items())[:2]
    )
    return f"[{tool_name}]{first_args} ({content_len:,} chars result)"


def _attachment_receipt_text(kind: str, name: str = "") -> str:
    """Build stable receipt text for a durable attachment reference."""
    label = f"{kind}: {name}" if name else kind
    return f"[Attached {label} — payload omitted; explicitly attach it again to reuse]"


def _attachment_receipt(part: dict[str, Any]) -> dict[str, str]:
    """Build a stable text receipt for a payload-bearing rich content part."""
    part_type = str(part.get("type") or "file")
    kind = _ATTACHMENT_KIND_BY_TYPE.get(part_type, "file")
    name = ""
    for key in ("filename", "file_name", "name", "title"):
        value = part.get(key)
        if isinstance(value, str) and value.strip():
            name = value.strip()
            break
    if not name:
        nested = part.get("file")
        if isinstance(nested, dict):
            for key in ("filename", "file_name", "name"):
                value = nested.get(key)
                if isinstance(value, str) and value.strip():
                    name = value.strip()
                    break
    return {"type": "text", "text": _attachment_receipt_text(kind, name)}


def project_attachment_content(content: Any) -> Any:
    """Return model-safe content with attachment bodies replaced by receipts.

    The projection understands current list-shaped rich content and legacy
    ``\0json:`` rows. It is non-mutating and idempotent; plain text and unknown
    content parts pass through unchanged.
    """
    decoded, was_serialized = _decode_structured_content(content)
    if not isinstance(decoded, list):
        return decoded if was_serialized else content

    for index, part in enumerate(decoded):
        if _is_attachment_part(part):
            projected = decoded[:index]
            break
    else:
        return decoded if was_serialized else content
    projected.extend(
        _attachment_receipt(part) if _is_attachment_part(part) else part
        for part in decoded[index:]
    )
    return projected


def compact_user_attachment_content(
    original_content: Any,
    attachments: Any,
) -> Any:
    """Build durable/follow-up content from clean user text and metadata."""
    if not isinstance(original_content, str):
        return project_attachment_content(original_content)
    receipts = []
    if isinstance(attachments, list):
        for attachment in attachments:
            if not isinstance(attachment, dict):
                continue
            kind = str(attachment.get("kind") or "file")
            name = str(attachment.get("name") or attachment.get("filename") or "").strip()
            receipts.append(_attachment_receipt_text(kind, name))
    parts = [original_content.strip(), *receipts]
    return "\n".join(part for part in parts if part)


def project_message_attachments(message: Any) -> Any:
    """Return a shallow-copied message with rich attachment payloads omitted."""
    if not isinstance(message, dict):
        return message
    content = message.get("content")
    projected = project_attachment_content(content)
    stashed = message.get("_anthropic_content_blocks")
    projected_stashed = project_attachment_content(stashed)
    if projected is content and projected_stashed is stashed:
        return message
    result = message.copy()
    if projected is not content:
        result["content"] = projected
    if projected_stashed is not stashed:
        result["_anthropic_content_blocks"] = projected_stashed
    return result


def project_historical_attachments(
    messages: list,
    *,
    preserve_index: int | None = None,
) -> list:
    """Project every attachment payload except an explicitly preserved message."""
    for index, message in enumerate(messages):
        next_message = message if index == preserve_index else project_message_attachments(message)
        if next_message is message:
            continue
        projected = messages[:index]
        projected.append(next_message)
        projected.extend(
            candidate
            if next_index == preserve_index
            else project_message_attachments(candidate)
            for next_index, candidate in enumerate(messages[index + 1 :], index + 1)
        )
        return projected
    return messages


def _tool_result_text(content: Any) -> tuple[str, bool]:
    """Return text usable by a receipt and whether rich payloads were omitted."""
    decoded, _ = _decode_structured_content(content)
    omitted_media = isinstance(decoded, list) and any(
        _is_attachment_part(part) for part in decoded
    )
    return flatten_message_text(decoded), omitted_media


def _tool_call_fields(tool_call: Any) -> tuple[str, str, str] | None:
    """Return a validated ``(id, name, arguments)`` tool-call tuple."""
    if not isinstance(tool_call, dict):
        return None
    call_id = tool_call.get("id")
    function = tool_call.get("function")
    if not isinstance(call_id, str) or not call_id or not isinstance(function, dict):
        return None
    name = function.get("name")
    arguments = function.get("arguments", "")
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(arguments, str):
        try:
            arguments = json.dumps(
                arguments, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
        except (TypeError, ValueError):
            return None
    return call_id, name, arguments


def _completed_tool_episode(
    messages: list,
    start: int,
    eligible_end: int,
) -> tuple[int, list[tuple[str, str, str, dict[str, Any]]], int | None] | None:
    """Parse complete contiguous tool rounds without crossing ``eligible_end``."""
    index = start
    records: list[tuple[str, str, str, dict[str, Any]]] = []
    while index < eligible_end:
        assistant = messages[index]
        if not isinstance(assistant, dict) or assistant.get("role") != "assistant":
            return None
        tool_calls = assistant.get("tool_calls")
        if not isinstance(tool_calls, list) or not tool_calls:
            return None

        parsed_calls = [_tool_call_fields(call) for call in tool_calls]
        if any(call is None for call in parsed_calls):
            return None
        calls = [call for call in parsed_calls if call is not None]
        call_ids = [call[0] for call in calls]
        call_id_set = set(call_ids)
        if len(call_id_set) != len(call_ids):
            return None

        result_by_id: dict[str, dict[str, Any]] = {}
        index += 1
        while index < eligible_end:
            result = messages[index]
            if not isinstance(result, dict) or result.get("role") != "tool":
                break
            result_id = result.get("tool_call_id")
            if (
                not isinstance(result_id, str)
                or result_id not in call_id_set
                or result_id in result_by_id
            ):
                return None
            result_by_id[result_id] = result
            index += 1

        if result_by_id.keys() != call_id_set:
            return None
        records.extend((*call, result_by_id[call[0]]) for call in calls)

        if index >= len(messages):
            return index, records, None
        following = messages[index]
        if not isinstance(following, dict):
            return None
        if following.get("role") == "assistant":
            if index >= eligible_end:
                return None
            if following.get("tool_calls"):
                continue
            return index + 1, records, index
        return index, records, None
    return None


def _tool_episode_summary(
    records: list[tuple[str, str, str, dict[str, Any]]],
) -> str:
    """Render a bounded deterministic provider-facing tool-episode summary."""
    lines = ["[Historical tool episode summary]"]
    for _, name, arguments, result in records:
        result_text, omitted_media = _tool_result_text(result.get("content"))
        receipt = summarize_tool_result(name, arguments, result_text)
        if len(receipt) > 500:
            receipt = receipt[:497] + "..."
        flags = []
        if result.get("is_error") or result.get("error"):
            flags.append("error")
        if omitted_media:
            flags.append("media_omitted")
        suffix = f"; flags={','.join(flags)}" if flags else ""
        lines.append(f"- tool={name[:80]}; result={receipt}{suffix}")
    return "\n".join(lines)


def _collapsed_tool_episode(
    messages: list,
    records: list[tuple[str, str, str, dict[str, Any]]],
    final_index: int | None,
) -> dict[str, Any]:
    """Build one ordinary assistant message for a completed old tool episode."""
    summary = _tool_episode_summary(records)
    if final_index is None:
        return {"role": "assistant", "content": summary}

    final = project_message_attachments(messages[final_index])
    final_content = final.get("content") if isinstance(final, dict) else None
    if isinstance(final_content, list):
        content = [{"type": "text", "text": summary}, *final_content]
    elif isinstance(final_content, str) and final_content.strip():
        content = f"{summary}\n\n{final_content}"
    else:
        content = summary
    return {"role": "assistant", "content": content}


def project_provider_history(
    messages: list,
    *,
    current_turn_index: int,
    protect_last_n: int,
) -> list:
    """Project provider history while preserving only current-turn attachments.

    The boundary is anchored to the current user row, so appending tool-loop rows
    does not move it. Complete old tool episodes become ordinary assistant text;
    every rich payload before the current user turn becomes a receipt even inside
    the protected text tail. Current-turn tool results remain available to the
    next model iteration. Canonical history is never mutated.
    """
    if not messages or current_turn_index <= 0:
        return messages
    current_turn_index = min(current_turn_index, len(messages) - 1)
    protected_start = max(0, current_turn_index - max(0, protect_last_n))

    projected: list[Any] = []
    changed = False
    index = 0
    while index < protected_start:
        message = messages[index]
        is_tool_start = (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and bool(message.get("tool_calls"))
        )
        if is_tool_start:
            episode = _completed_tool_episode(messages, index, protected_start)
            if episode is not None:
                end, records, final_index = episode
                previous_role = messages[index - 1].get("role") if index else None
                following_role = (
                    messages[end].get("role")
                    if end < len(messages) and isinstance(messages[end], dict)
                    else None
                )
                if previous_role == "user" and following_role in {"user", None}:
                    projected.append(
                        _collapsed_tool_episode(messages, records, final_index)
                    )
                    index = end
                    changed = True
                    continue

        if isinstance(message, dict) and (
            message.get("role") == "tool" or message.get("tool_calls")
        ):
            next_message = message
        else:
            next_message = project_message_attachments(message)
        projected.append(next_message)
        changed = changed or next_message is not message
        index += 1

    protected_tail = messages[protected_start:current_turn_index]
    projected_tail = [project_message_attachments(message) for message in protected_tail]
    tail_changed = any(
        next_message is not message
        for message, next_message in zip(protected_tail, projected_tail)
    )
    projected.extend(projected_tail)
    projected.extend(messages[current_turn_index:])
    return projected if changed or tail_changed else messages


def _strip_images_from_messages(messages: list) -> bool:
    """Remove image_url content parts from all messages in-place.

    Called when a server signals it does not support images (e.g.
    "Only 'text' content type is supported.").  Mutates messages so the
    next API call sends text only.

    Preserves message alternation invariants:
      * ``tool``-role messages whose content was entirely images are replaced
        with a plaintext placeholder, NOT deleted — deleting them would leave
        the paired ``tool_call_id`` on the prior assistant message unmatched,
        which providers reject with HTTP 400.
      * Non-tool messages whose content becomes empty are dropped.  In
        practice this only hits synthetic image-only user messages appended
        for attachment delivery; real user turns always include text.

    Returns True if any image parts were removed.
    """
    found = False
    to_delete = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        new_parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"image_url", "image", "input_image"}:
                found = True
            else:
                new_parts.append(part)
        if len(new_parts) < len(content):
            if new_parts:
                msg["content"] = new_parts
            elif msg.get("role") == "tool":
                # Preserve tool_call_id linkage — providers require every
                # assistant tool_call to have a matching tool response.
                msg["content"] = "[image content removed — server does not support images]"
            else:
                # Synthetic image-only user/assistant message with no text;
                # safe to drop.
                to_delete.append(i)
    for i in reversed(to_delete):
        del messages[i]
    return found


def _sanitize_structure_non_ascii(payload: Any) -> bool:
    """Strip non-ASCII characters from nested dict/list payloads in-place."""
    found = False

    def _walk(node):
        nonlocal found
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str):
                    sanitized = _strip_non_ascii(value)
                    if sanitized != value:
                        node[key] = sanitized
                        found = True
                elif isinstance(value, (dict, list)):
                    _walk(value)
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                if isinstance(value, str):
                    sanitized = _strip_non_ascii(value)
                    if sanitized != value:
                        node[idx] = sanitized
                        found = True
                elif isinstance(value, (dict, list)):
                    _walk(value)

    _walk(payload)
    return found


__all__ = [
    "_SURROGATE_RE",
    "close_interrupted_tool_sequence",
    "_sanitize_surrogates",
    "_sanitize_structure_surrogates",
    "_sanitize_messages_surrogates",
    "_escape_invalid_chars_in_json_strings",
    "_repair_tool_call_arguments",
    "_strip_non_ascii",
    "_sanitize_messages_non_ascii",
    "_sanitize_tools_non_ascii",
    "_decode_structured_content",
    "_is_attachment_part",
    "project_attachment_content",
    "compact_user_attachment_content",
    "project_message_attachments",
    "project_historical_attachments",
    "project_provider_history",
    "summarize_tool_result",
    "_strip_images_from_messages",
    "_sanitize_structure_non_ascii",
]
