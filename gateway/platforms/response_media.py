"""Owner-neutral response parsing and local-file admission for native delivery."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

MEDIA_DELIVERY_EXTS: tuple[str, ...] = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg",
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".mp3", ".wav", ".ogg", ".opus", ".m4a", ".flac",
    ".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt", ".md", ".epub",
    ".xlsx", ".xls", ".ods", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml",
    ".pptx", ".ppt", ".odp", ".key",
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".apk", ".ipa",
    ".html", ".htm",
)
_MEDIA_EXT_ALTERNATION = "|".join(
    sorted((extension.lstrip(".") for extension in MEDIA_DELIVERY_EXTS), key=len, reverse=True)
)
MEDIA_TAG_CLEANUP_RE = re.compile(
    r'''[`"']?MEDIA:\s*'''
    r'''(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|'''
    r'''(?:~/|/|[A-Za-z]:[/\\])\S+(?:[^\S\n]+\S+)*?\.(?:''' + _MEDIA_EXT_ALTERNATION + r'''))'''
    r'''(?=[\s`"',;:)\]}]|$)[`"']?''',
    re.IGNORECASE,
)
MEDIA_EXTENSIONLESS_TAG_RE = re.compile(
    r'''[`"']?MEDIA:\s*'''
    r'''(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|'''
    r'''(?:~/|/|[A-Za-z]:[/\\])[^\s\n`"']+)'''
    r'''[`"']?\s*''',
    re.IGNORECASE,
)
_LOG_UNSAFE_CHARS = re.compile(r"[\x00-\x1f\x7f\x85  ]")


def _log_safe_path(path: str) -> str:
    return _LOG_UNSAFE_CHARS.sub("?", str(path))[:200]


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_media_delivery_path(
    path: str,
    *,
    allowed_roots: Iterable[str | Path],
) -> str | None:
    """Resolve a regular file only when it stays within an explicit trusted root."""
    if not path:
        return None
    candidate = str(path).strip()
    if len(candidate) >= 2 and candidate[0] == candidate[-1] and candidate[0] in "`\"'":
        candidate = candidate[1:-1].strip()
    candidate = candidate.lstrip("`\"'").rstrip("`\"',.;:)}]")
    if not candidate:
        return None
    try:
        expanded = Path(os.path.expanduser(candidate))
    except (OSError, RuntimeError, ValueError):
        return None
    if not expanded.is_absolute():
        return None
    try:
        resolved = expanded.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return None
    if not resolved.is_file():
        return None
    for root in allowed_roots:
        root_path = Path(root).expanduser()
        try:
            resolved_root = root_path.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            continue
        if root_path.is_symlink() or not resolved_root.is_dir():
            continue
        if _path_is_within(resolved, resolved_root):
            return str(resolved)
    return None


def extract_images(content: str) -> tuple[list[tuple[str, str]], str]:
    """Extract remotely hosted image tags while preserving non-image links."""
    images: list[tuple[str, str]] = []
    markdown = r'!\[([^\]]*)\]\((https?://[^\s\)]+)\)'
    html = r'<img\s+src=["\']?(https?://[^\s"\'<>]+)["\']?\s*/?>\s*(?:</img>)?'
    for match in re.finditer(markdown, content):
        url = match.group(2)
        if any(
            url.lower().endswith(extension) or extension in url.lower()
            for extension in (
                ".png", ".jpg", ".jpeg", ".gif", ".webp",
                "fal.media", "fal-cdn", "replicate.delivery",
            )
        ):
            images.append((url, match.group(1)))
    for match in re.finditer(html, content):
        images.append((match.group(1), ""))
    if not images:
        return images, content
    extracted = {url for url, _alt in images}

    def remove(match: re.Match[str]) -> str:
        url = match.group(2) if match.lastindex and match.lastindex >= 2 else match.group(1)
        return "" if url in extracted else match.group(0)

    cleaned = re.sub(markdown, remove, content)
    cleaned = re.sub(html, remove, cleaned)
    return images, re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _normalize_media_tag_path(raw: str) -> str:
    path = str(raw or "").strip()
    if len(path) >= 2 and path[0] == path[-1] and path[0] in "`\"'":
        path = path[1:-1].strip()
    return path.lstrip("`\"'").rstrip("`\"',.;:)}]")


def _path_lacks_deliverable_extension(path: str) -> bool:
    return not Path(path).suffix


def _mask_protected_spans(content: str) -> str:
    chars = list(content)
    spans = [
        match.span() for match in re.finditer(r"```[^\n]*\n.*?```", content, re.DOTALL)
    ]
    for match in re.finditer(r"`[^`\n]+`", content):
        if re.search(r"MEDIA:\s*$", content[max(0, match.start() - 20):match.start()]):
            continue
        spans.append(match.span())
    spans.extend(match.span() for match in re.finditer(r"^>.*$", content, re.MULTILINE))
    for start, end in spans:
        for index in range(start, end):
            if chars[index] != "\n":
                chars[index] = " "
    return "".join(chars)


def _mask_json_string_media(content: str) -> str:
    if '"' not in content or "MEDIA:" not in content:
        return content
    chars = list(content)
    for match in re.finditer(r'(?<=[:,{\[])\s*"((?:[^"\\\n]|\\.)*)"', content):
        if re.search(r'MEDIA:\s*(?:~/|/|[A-Za-z]:[/\\])', match.group(1)):
            for index in range(match.start(1), match.end(1)):
                if chars[index] != "\n":
                    chars[index] = " "
    return "".join(chars)


def extract_media(
    content: str,
    *,
    path_validator: Callable[[str], str | None] | None = None,
) -> tuple[list[tuple[str, bool]], str]:
    """Extract explicit MEDIA tags without consulting ambient Hermes state."""
    media: list[tuple[str, bool]] = []
    has_voice_tag = "[[audio_as_voice]]" in content
    cleaned = content.replace("[[audio_as_voice]]", "").replace("[[as_document]]", "")
    scan_content = _mask_json_string_media(_mask_protected_spans(content))
    for match in MEDIA_TAG_CLEANUP_RE.finditer(scan_content):
        path = _normalize_media_tag_path(match.group("path"))
        if path:
            try:
                media.append((os.path.expanduser(path), has_voice_tag))
            except (OSError, RuntimeError, ValueError):
                continue
    seen = {path for path, _is_voice in media}
    if path_validator is not None:
        for match in MEDIA_EXTENSIONLESS_TAG_RE.finditer(scan_content):
            path = _normalize_media_tag_path(match.group("path"))
            if not path or not _path_lacks_deliverable_extension(path):
                continue
            safe = path_validator(path)
            if safe and safe not in seen:
                media.append((safe, has_voice_tag))
                seen.add(safe)
    if media:
        masked_cleaned = _mask_json_string_media(_mask_protected_spans(cleaned))
        spans = [match.span() for match in MEDIA_TAG_CLEANUP_RE.finditer(masked_cleaned)]
        if path_validator is not None:
            for match in MEDIA_EXTENSIONLESS_TAG_RE.finditer(masked_cleaned):
                path = _normalize_media_tag_path(match.group("path"))
                if path and _path_lacks_deliverable_extension(path) and path_validator(path):
                    spans.append(match.span())
        if spans:
            chars = list(cleaned)
            for start, end in sorted(spans, reverse=True):
                del chars[start:end]
            cleaned = re.sub(r"\n{3,}", "\n\n", "".join(chars)).strip()
    return media, cleaned


def extract_local_files(content: str) -> tuple[list[str], str]:
    """Extract existing bare local paths while ignoring code and URLs."""
    extensions = "|".join(extension.lstrip(".") for extension in MEDIA_DELIVERY_EXTS)
    path_pattern = re.compile(
        r"(?<![/:\w.])(?:~/|/|[A-Za-z]:[/\\])(?:[\w.\-]+[/\\])*[\w.\-]+\.(?:"
        + extensions
        + r")\b",
        re.IGNORECASE,
    )
    code_spans = [
        match.span() for match in re.finditer(r"```[^\n]*\n.*?```", content, re.DOTALL)
    ]
    code_spans.extend(match.span() for match in re.finditer(r"`[^`\n]+`", content))
    found: list[tuple[str, str]] = []
    for match in path_pattern.finditer(content):
        if any(start <= match.start() < end for start, end in code_spans):
            continue
        raw = match.group(0)
        expanded = os.path.expanduser(raw)
        if os.path.isfile(expanded):
            found.append((raw, expanded))
        else:
            logger.info(
                "Skipping bare file path in reply (no file on disk): %s",
                _log_safe_path(raw),
            )
    unique: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw, expanded in found:
        if expanded not in seen:
            seen.add(expanded)
            unique.append((raw, expanded))
    cleaned = content
    for raw, _expanded in unique:
        cleaned = cleaned.replace(raw, "")
    if unique:
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return [expanded for _raw, expanded in unique], cleaned
