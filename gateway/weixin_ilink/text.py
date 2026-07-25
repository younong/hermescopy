"""Shared formatting and bounded text delivery for WeChat iLink."""

from __future__ import annotations

import re
import textwrap

from gateway.platforms.base import BasePlatformAdapter

ILINK_TEXT_MESSAGE_LIMIT = 2000
WEIXIN_COPY_LINE_WIDTH = 120

_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_TABLE_RULE_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
_FENCE_RE = re.compile(r"^```([^\n`]*)\s*$")


def format_weixin_text(content: str) -> str:
    return _wrap_copy_friendly_lines(_normalize_markdown_blocks(content))


def split_weixin_text(
    content: str,
    max_length: int = ILINK_TEXT_MESSAGE_LIMIT,
    *,
    split_per_line: bool = False,
) -> list[str]:
    """Split formatted content into sequential, platform-bounded messages."""
    if not content:
        return []
    if split_per_line:
        if len(content) <= max_length and "\n" not in content:
            return [content]
        chunks: list[str] = []
        for unit in _split_delivery_units(content):
            if len(unit) <= max_length:
                chunks.append(unit)
                continue
            chunks.extend(_pack_markdown_blocks(unit, max_length))
        return [chunk for chunk in chunks if chunk] or [content]

    if len(content) <= max_length:
        return (
            [unit for unit in _split_delivery_units(content) if unit]
            if _should_split_short_chat_block(content)
            else [content]
        )
    return _pack_markdown_blocks(content, max_length) or [content]


def _normalize_markdown_blocks(content: str) -> str:
    lines = content.splitlines()
    result: list[str] = []
    in_code_block = False
    blank_run = 0

    for raw_line in lines:
        line = raw_line.rstrip()
        if _FENCE_RE.match(line.strip()):
            in_code_block = not in_code_block
            result.append(line)
            blank_run = 0
            continue
        if in_code_block:
            result.append(line)
            continue
        if not line.strip():
            blank_run += 1
            if blank_run <= 1:
                result.append("")
            continue
        blank_run = 0
        result.append(line)
    return "\n".join(result).strip()


def _wrap_copy_friendly_lines(content: str) -> str:
    if not content:
        return content

    wrapped: list[str] = []
    in_code_block = False
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if _FENCE_RE.match(stripped):
            in_code_block = not in_code_block
            wrapped.append(line)
            continue
        if (
            in_code_block
            or len(line) <= WEIXIN_COPY_LINE_WIDTH
            or not stripped
            or stripped.startswith("|")
            or _TABLE_RULE_RE.match(stripped)
        ):
            wrapped.append(line)
            continue
        wrapped.extend(
            textwrap.wrap(
                line,
                width=WEIXIN_COPY_LINE_WIDTH,
                break_long_words=False,
                break_on_hyphens=False,
                replace_whitespace=False,
                drop_whitespace=True,
            )
            or [line]
        )
    return "\n".join(wrapped).strip()


def _split_markdown_blocks(content: str) -> list[str]:
    if not content:
        return []

    blocks: list[str] = []
    current: list[str] = []
    in_code_block = False
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if _FENCE_RE.match(line.strip()):
            if not in_code_block and current:
                blocks.append("\n".join(current).strip())
                current = []
            current.append(line)
            in_code_block = not in_code_block
            if not in_code_block:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        if in_code_block:
            current.append(line)
            continue
        if not line.strip():
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [block for block in blocks if block]


def _split_delivery_units(content: str) -> list[str]:
    units: list[str] = []
    for block in _split_markdown_blocks(content):
        if _FENCE_RE.match(block.splitlines()[0].strip()):
            units.append(block)
            continue
        current: list[str] = []
        for raw_line in block.splitlines():
            line = raw_line.rstrip()
            if not line.strip():
                if current:
                    units.append("\n".join(current).strip())
                    current = []
                continue
            if current and raw_line.startswith((" ", "\t")):
                current.append(line)
                continue
            if current:
                units.append("\n".join(current).strip())
            current = [line]
        if current:
            units.append("\n".join(current).strip())
    return [unit for unit in units if unit]


def _looks_like_chatty_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 48 or line.startswith((" ", "\t")):
        return False
    if stripped.startswith((">", "-", "*", "【", "#", "|")):
        return False
    if _TABLE_RULE_RE.match(stripped) or re.match(r"^\*\*[^*]+\*\*$", stripped):
        return False
    return re.match(r"^\d+\.\s", stripped) is None


def _looks_like_heading_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return bool(_HEADER_RE.match(stripped)) or (
        len(stripped) <= 24 and stripped.endswith((":", "："))
    )


def _should_split_short_chat_block(block: str) -> bool:
    lines = [line for line in block.splitlines() if line.strip()]
    if not 2 <= len(lines) <= 6 or _looks_like_heading_line(lines[0]):
        return False
    return all(_looks_like_chatty_line(line) for line in lines)


def _pack_markdown_blocks(content: str, max_length: int) -> list[str]:
    if len(content) <= max_length:
        return [content]

    packed: list[str] = []
    current = ""
    for block in _split_markdown_blocks(content):
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= max_length:
            current = candidate
            continue
        if current:
            packed.append(current)
            current = ""
        if len(block) <= max_length:
            current = block
            continue
        packed.extend(BasePlatformAdapter.truncate_message(block, max_length))
    if current:
        packed.append(current)
    return packed
