"""One-shot authenticated relay for exact owner-scoped tool execution.

The Tool Executor receives no owner credentials or owner-home mount. An exact
allowlisted invocation may receive one socket endpoint; the owner worker checks
the invocation binding and dispatches the operation in its owner-scoped runtime.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
import struct
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from hermes_cli.controlled_roots import ExpectedType, RootKind
from hermes_cli.owner_worker.executor_identity import (
    EgressProfile,
    ExecutorIdentity,
    ExecutorInvocation,
)

logger = logging.getLogger(__name__)

OWNER_FILE_TOOL_NAMES = frozenset({
    "read_file", "write_file", "patch", "search_files",
})
_OWNER_MEDIA_TOOL_NAMES = frozenset({
    "image_generate", "text_to_speech", "video_generate",
    "xai_video_edit", "xai_video_extend",
})
# Media tools whose active selection may match a deployment route: the broker
# prefers the media dispatcher (deployment relay) and falls back to local
# plugin execution with the owner's own credentials.
_DEPLOYMENT_CAPABLE_MEDIA_TOOL_NAMES = frozenset({"image_generate", "video_generate"})
_OWNER_ALWAYS_RELAY_TOOL_NAMES = frozenset({
    "web_search", "web_extract", "skills_list", "skill_view",
}) | _OWNER_MEDIA_TOOL_NAMES
OWNER_RELAY_TOOL_NAMES = _OWNER_ALWAYS_RELAY_TOOL_NAMES | OWNER_FILE_TOOL_NAMES
_MAX_REQUEST_BYTES = 256 * 1024
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_MEDIA_BYTES = 512 * 1024 * 1024
_MEDIA_SUFFIXES = {
    "audio": frozenset({".mp3", ".ogg", ".wav", ".flac", ".m4a", ".aac"}),
    "image": frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"}),
    "video": frozenset({".mp4", ".webm", ".mov", ".m4v"}),
}


class OwnerToolRelayError(RuntimeError):
    """The private owner tool relay rejected or lost an invocation."""


def _send_frame(connection: socket.socket, value: dict[str, Any], *, limit: int) -> None:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if not encoded or len(encoded) > limit:
        raise OwnerToolRelayError("owner tool relay frame is invalid")
    connection.sendall(struct.pack("!I", len(encoded)) + encoded)


def _recv_exact(connection: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise OwnerToolRelayError("owner tool relay peer closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_frame(connection: socket.socket, *, limit: int) -> dict[str, Any]:
    size = struct.unpack("!I", _recv_exact(connection, 4))[0]
    if not size or size > limit:
        raise OwnerToolRelayError("owner tool relay frame is invalid")
    try:
        value = json.loads(_recv_exact(connection, size))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OwnerToolRelayError("owner tool relay frame is malformed") from exc
    if not isinstance(value, dict):
        raise OwnerToolRelayError("owner tool relay frame is malformed")
    return value


def _validated_arguments(tool_name: str, arguments: object) -> dict[str, Any]:
    if not isinstance(arguments, dict):
        raise OwnerToolRelayError("owner tool relay arguments are invalid")
    if tool_name == "web_search":
        if set(arguments) - {"query", "limit"}:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        query = arguments.get("query")
        limit = arguments.get("limit", 5)
        if not isinstance(query, str) or not query.strip() or len(query) > 16_384:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        return {"query": query, "limit": limit}
    if tool_name == "web_extract":
        if set(arguments) - {"urls", "char_limit"}:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        urls = arguments.get("urls")
        char_limit = arguments.get("char_limit")
        if (
            not isinstance(urls, list)
            or not 1 <= len(urls) <= 5
            or any(not isinstance(url, str) or not url.strip() or len(url) > 16_384 for url in urls)
        ):
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        if char_limit is not None and (
            not isinstance(char_limit, int)
            or isinstance(char_limit, bool)
            or char_limit < 2_000
            or char_limit > 500_000
        ):
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        result: dict[str, Any] = {"urls": list(urls)}
        if char_limit is not None:
            result["char_limit"] = char_limit
        return result
    if tool_name == "read_file":
        if set(arguments) - {"path", "offset", "limit"}:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        path = arguments.get("path")
        offset = arguments.get("offset", 1)
        limit = arguments.get("limit", 500)
        if not isinstance(path, str) or not path or len(path) > 4096 or "\x00" in path:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 1:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 2000:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        return {"path": path, "offset": offset, "limit": limit}
    if tool_name == "write_file":
        if set(arguments) - {"path", "content", "cross_profile"}:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        path = arguments.get("path")
        content = arguments.get("content")
        cross_profile = arguments.get("cross_profile", False)
        if not isinstance(path, str) or not path or len(path) > 4096 or "\x00" in path:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        if not isinstance(content, str) or not isinstance(cross_profile, bool):
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        return {"path": path, "content": content, "cross_profile": cross_profile}
    if tool_name == "patch":
        allowed = {"mode", "path", "old_string", "new_string", "replace_all", "patch", "cross_profile"}
        if set(arguments) - allowed:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        mode = arguments.get("mode", "replace")
        if mode not in {"replace", "patch"}:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        if mode == "replace":
            required = (arguments.get("path"), arguments.get("old_string"), arguments.get("new_string"))
            if (
                not isinstance(required[0], str)
                or not required[0]
                or len(required[0]) > 4096
                or "\x00" in required[0]
                or not isinstance(required[1], str)
                or not isinstance(required[2], str)
            ):
                raise OwnerToolRelayError("owner tool relay arguments are invalid")
        elif not isinstance(arguments.get("patch"), str) or not arguments["patch"]:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        if not isinstance(arguments.get("replace_all", False), bool) or not isinstance(
            arguments.get("cross_profile", False), bool
        ):
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        return dict(arguments, mode=mode)
    if tool_name == "search_files":
        allowed = {"pattern", "target", "path", "file_glob", "limit", "offset", "output_mode", "context"}
        if set(arguments) - allowed:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        pattern = arguments.get("pattern")
        target = arguments.get("target", "content")
        path = arguments.get("path", ".")
        file_glob = arguments.get("file_glob")
        limit = arguments.get("limit", 50)
        offset = arguments.get("offset", 0)
        output_mode = arguments.get("output_mode", "content")
        context = arguments.get("context", 0)
        if not isinstance(pattern, str) or len(pattern) > 16_384 or "\x00" in pattern:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        if target not in {"content", "files"} or output_mode not in {"content", "files_only", "count"}:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        if not isinstance(path, str) or not path or len(path) > 4096 or "\x00" in path:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        if file_glob is not None and (not isinstance(file_glob, str) or len(file_glob) > 4096 or "\x00" in file_glob):
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10_000:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        if not isinstance(context, int) or isinstance(context, bool) or context < 0:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        return {
            "pattern": pattern, "target": target, "path": path,
            **({"file_glob": file_glob} if file_glob is not None else {}),
            "limit": limit, "offset": offset, "output_mode": output_mode, "context": context,
        }
    if tool_name == "text_to_speech":
        if set(arguments) - {"text"}:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 40_000 or "\x00" in text:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        return {"text": text}
    if tool_name == "video_generate":
        allowed = {
            "prompt", "image_url", "reference_image_urls", "duration", "aspect_ratio",
            "resolution", "negative_prompt", "audio", "seed", "model",
        }
        if set(arguments) - allowed:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32_768 or "\x00" in prompt:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        result: dict[str, Any] = {"prompt": prompt}
        for key in ("image_url", "negative_prompt", "model"):
            value = arguments.get(key)
            if value is not None:
                if not isinstance(value, str) or not value.strip() or len(value) > 16_384 or "\x00" in value:
                    raise OwnerToolRelayError("owner tool relay arguments are invalid")
                result[key] = value
        references = arguments.get("reference_image_urls")
        if references is not None:
            if not isinstance(references, list) or len(references) > 16 or any(
                not isinstance(value, str) or not value.strip() or len(value) > 16_384 or "\x00" in value
                for value in references
            ):
                raise OwnerToolRelayError("owner tool relay arguments are invalid")
            result["reference_image_urls"] = list(references)
        for key in ("duration", "seed"):
            value = arguments.get(key)
            if value is not None:
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    raise OwnerToolRelayError("owner tool relay arguments are invalid")
                result[key] = value
        aspect_ratio = arguments.get("aspect_ratio")
        if aspect_ratio is not None:
            from agent.video_gen_provider import COMMON_ASPECT_RATIOS
            if aspect_ratio not in COMMON_ASPECT_RATIOS:
                raise OwnerToolRelayError("owner tool relay arguments are invalid")
            result["aspect_ratio"] = aspect_ratio
        resolution = arguments.get("resolution")
        if resolution is not None:
            from agent.video_gen_provider import COMMON_RESOLUTIONS
            if resolution not in COMMON_RESOLUTIONS:
                raise OwnerToolRelayError("owner tool relay arguments are invalid")
            result["resolution"] = resolution
        audio = arguments.get("audio")
        if audio is not None:
            if not isinstance(audio, bool):
                raise OwnerToolRelayError("owner tool relay arguments are invalid")
            result["audio"] = audio
        return result
    if tool_name in {"xai_video_edit", "xai_video_extend"}:
        allowed = {"prompt", "video_url", "model"}
        if tool_name == "xai_video_extend":
            allowed.add("duration")
        if set(arguments) - allowed:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        prompt = arguments.get("prompt")
        video_url = arguments.get("video_url")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32_768 or "\x00" in prompt:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        if not isinstance(video_url, str) or len(video_url) > 16_384 or "\x00" in video_url:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        parsed = urlparse(video_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        result = {"prompt": prompt, "video_url": video_url}
        model = arguments.get("model")
        if model is not None:
            if not isinstance(model, str) or not model.strip() or len(model) > 1024 or "\x00" in model:
                raise OwnerToolRelayError("owner tool relay arguments are invalid")
            result["model"] = model
        if tool_name == "xai_video_extend" and arguments.get("duration") is not None:
            duration = arguments["duration"]
            if not isinstance(duration, int) or isinstance(duration, bool) or duration < 0:
                raise OwnerToolRelayError("owner tool relay arguments are invalid")
            result["duration"] = duration
        return result
    if tool_name == "image_generate":
        if set(arguments) - {"prompt", "aspect_ratio", "resolution", "image_url", "reference_image_urls"}:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        prompt = arguments.get("prompt")
        aspect_ratio = arguments.get("aspect_ratio", "landscape")
        resolution = arguments.get("resolution", "2K")
        image_url = arguments.get("image_url")
        references = arguments.get("reference_image_urls")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32_768 or "\x00" in prompt:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        if aspect_ratio not in {"landscape", "square", "portrait"}:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        if resolution not in {"1K", "2K", "4K"}:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        if image_url is not None and (not isinstance(image_url, str) or not image_url.strip() or len(image_url) > 4096 or "\x00" in image_url):
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        if references is not None and (not isinstance(references, list) or len(references) > 16 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 4096 or "\x00" in item for item in references
        )):
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        result = {"prompt": prompt.strip(), "aspect_ratio": aspect_ratio, "resolution": resolution}
        if image_url is not None:
            result["image_url"] = image_url.strip()
        if references is not None:
            result["reference_image_urls"] = [item.strip() for item in references]
        return result
    if tool_name == "skills_list":
        if set(arguments) - {"category"}:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        category = arguments.get("category")
        if category is not None and (
            not isinstance(category, str)
            or not category.strip()
            or len(category) > 256
            or "\x00" in category
        ):
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        return {} if category is None else {"category": category}
    if tool_name == "skill_view":
        if set(arguments) - {"name", "file_path"}:
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        name = arguments.get("name")
        file_path = arguments.get("file_path")
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name) > 1024
            or "\x00" in name
        ):
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        if file_path is not None and (
            not isinstance(file_path, str)
            or not file_path.strip()
            or len(file_path) > 4096
            or "\x00" in file_path
        ):
            raise OwnerToolRelayError("owner tool relay arguments are invalid")
        result = {"name": name}
        if file_path is not None:
            result["file_path"] = file_path
        return result
    raise OwnerToolRelayError("owner tool relay operation is not allowed")


def owner_file_tool_relay_admissible(invocation: ExecutorInvocation) -> bool:
    """Return whether an exact native file invocation can use the owner relay."""
    if (
        invocation.tool_name not in OWNER_FILE_TOOL_NAMES
        or invocation.egress_profile is not EgressProfile.TOOL_NONE
    ):
        return False
    try:
        _validated_arguments(invocation.tool_name, invocation.arguments)
    except OwnerToolRelayError:
        return False
    return True


def _media_path_from_result(result: dict[str, Any], *, category: str) -> str:
    field = {"audio": "file_path", "image": "image"}.get(category, "video")
    raw = result.get(field)
    if not isinstance(raw, str) or not raw:
        raise OwnerToolRelayError("owner media tool did not produce an artifact")
    return raw


def _media_content_is_valid(data: bytes, suffix: str) -> bool:
    if suffix == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return data.startswith(b"\xff\xd8\xff")
    if suffix == ".webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    if suffix == ".gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".mp3":
        return data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0)
    if suffix == ".ogg":
        return data.startswith(b"OggS")
    if suffix == ".wav":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE"
    if suffix == ".flac":
        return data.startswith(b"fLaC")
    if suffix == ".aac":
        return len(data) >= 2 and data[0] == 0xFF and data[1] & 0xF6 == 0xF0
    if suffix in {".m4a", ".mp4", ".mov", ".m4v"}:
        return len(data) >= 12 and data[4:8] == b"ftyp"
    if suffix == ".webm":
        return data.startswith(b"\x1aE\xdf\xa3")
    return False


def _read_media_file(
    path: Path,
    *,
    category: str,
    staging_root: Path,
    workspace_context: Any,
) -> tuple[bytes, str]:
    suffix = path.suffix.lower()
    if suffix not in _MEDIA_SUFFIXES[category]:
        raise OwnerToolRelayError("owner media artifact type is invalid")

    fd = -1
    try:
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(staging_root.resolve(strict=True))
        except (OSError, ValueError):
            if category not in {"image", "video"} or not path.is_absolute():
                raise OwnerToolRelayError(
                    "owner media artifact is outside controlled staging"
                ) from None
            owner_root = workspace_context.roots.get(RootKind.OWNER_WRITABLE).canonical_path
            try:
                owner_relative = path.relative_to(owner_root).as_posix()
            except ValueError:
                raise OwnerToolRelayError(
                    "owner media artifact is outside controlled staging"
                ) from None
            components = owner_relative.split("/")
            if category == "image":
                cache_ok = len(components) == 2 and components[0] == "images"
            else:
                cache_ok = len(components) == 3 and components[:2] == ["cache", "videos"]
            if not cache_ok or any(component in {"", ".", ".."} for component in components):
                raise OwnerToolRelayError(
                    "owner media artifact is outside controlled staging"
                )
            fd = workspace_context.roots.open_relative(
                RootKind.OWNER_WRITABLE,
                owner_relative,
                expected_type=ExpectedType.REGULAR_FILE,
            )
        else:
            fd = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))

        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_MEDIA_BYTES
        ):
            raise OwnerToolRelayError("owner media artifact is invalid")
        data = bytearray()
        while len(data) <= _MAX_MEDIA_BYTES:
            chunk = os.read(fd, min(1024 * 1024, _MAX_MEDIA_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) != metadata.st_size or not _media_content_is_valid(data, suffix):
            raise OwnerToolRelayError("owner media artifact content is invalid")
        return bytes(data), path.name
    except OSError as exc:
        raise OwnerToolRelayError("owner media artifact is invalid") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _download_media(url: str, *, category: str, staging_root: Path) -> Path:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise OwnerToolRelayError("owner media artifact URL is invalid")
    suffix = Path(parsed.path).suffix.lower()
    if suffix not in _MEDIA_SUFFIXES[category]:
        suffix = ".mp4" if category == "video" else ".png"
    destination = staging_root / f"{category}{suffix}"
    try:
        import httpx
        from tools.url_safety import is_safe_url, redirect_target_from_response

        if not is_safe_url(url):
            raise OwnerToolRelayError("owner media artifact URL is unsafe")

        def _reject_unsafe_redirect(response: Any) -> None:
            redirect_url = redirect_target_from_response(response)
            if redirect_url and not is_safe_url(redirect_url):
                raise OwnerToolRelayError("owner media artifact redirect is unsafe")

        with httpx.stream(
            "GET",
            url,
            follow_redirects=True,
            timeout=120.0,
            event_hooks={"response": [_reject_unsafe_redirect]},
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type and not (
                content_type.startswith(f"{category}/")
                or content_type == "application/octet-stream"
            ):
                raise OwnerToolRelayError("owner media artifact content type is invalid")
            total = 0
            with destination.open("xb") as output:
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > _MAX_MEDIA_BYTES:
                        raise OwnerToolRelayError("owner media artifact is too large")
                    output.write(chunk)
    except OwnerToolRelayError:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise OwnerToolRelayError("owner media artifact download failed") from exc
    return destination


def _rewrite_media_result(
    raw_result: str,
    result: dict[str, Any],
    *,
    category: str,
    staging_root: Path,
    workspace_context: Any,
) -> str:
    if result.get("success") is not True:
        return raw_result
    source_value = _media_path_from_result(result, category=category)
    source = (
        _download_media(source_value, category=category, staging_root=staging_root)
        if category in {"image", "video"}
        and source_value.lower().startswith(("http://", "https://"))
        else Path(source_value)
    )
    data, filename = _read_media_file(
        source,
        category=category,
        staging_root=staging_root,
        workspace_context=workspace_context,
    )
    from hermes_cli.owner_worker.user_files import publish_unique_user_bytes

    published = publish_unique_user_bytes(workspace_context, category, filename, data)
    diagnostic = str(published.diagnostic_path)
    if category == "audio":
        result["file_path"] = diagnostic
        voice_prefix = "[[audio_as_voice]]\n" if result.get("voice_compatible") is True else ""
        result["media_tag"] = f"{voice_prefix}MEDIA:{diagnostic}"
    elif category == "image":
        result["image"] = diagnostic
    else:
        result["video"] = diagnostic
        result["media_tag"] = f"MEDIA:{diagnostic}"
    return json.dumps(result, ensure_ascii=False)


def _dispatch_owner_media_tool(
    tool_name: str,
    arguments: dict[str, Any],
    workspace_context: Any,
) -> str:
    if workspace_context is None:
        raise OwnerToolRelayError("owner media relay workspace is unavailable")
    from hermes_cli.config import reload_env
    reload_env()

    temporary_root = workspace_context.roots.get(RootKind.TEMPORARY).canonical_path
    with tempfile.TemporaryDirectory(
        prefix=f"{tool_name}-",
        dir=temporary_root,
    ) as directory:
        staging_root = Path(directory)
        if tool_name == "text_to_speech":
            from tools.tts_tool import text_to_speech_tool
            raw_result = text_to_speech_tool(
                arguments["text"], output_path=str(staging_root / "speech.mp3")
            )
            category = "audio"
        else:
            if tool_name == "image_generate":
                from tools.image_generation_tool import _handle_image_generate
                raw_result = _handle_image_generate(dict(arguments))
                category = "image"
            elif tool_name == "video_generate":
                from tools.video_generation_tool import _handle_video_generate
                raw_result = _handle_video_generate(dict(arguments))
                category = "video"
            else:
                from tools.xai_video_tools import _handle_xai_video_edit, _handle_xai_video_extend
                handler = (
                    _handle_xai_video_edit
                    if tool_name == "xai_video_edit"
                    else _handle_xai_video_extend
                )
                raw_result = handler(dict(arguments))
                category = "video"
        raw_result = str(raw_result)
        try:
            result_payload = json.loads(raw_result)
        except json.JSONDecodeError as exc:
            raise OwnerToolRelayError("owner media tool result is invalid") from exc
        if not isinstance(result_payload, dict):
            raise OwnerToolRelayError("owner media tool result is invalid")
        cache_source = (
            _media_path_from_result(result_payload, category=category)
            if category in {"image", "video"} and result_payload.get("success") is True
            else None
        )
        result = _rewrite_media_result(
            raw_result,
            result_payload,
            category=category,
            staging_root=staging_root,
            workspace_context=workspace_context,
        )
        if cache_source is not None:
            source_path = Path(cache_source)
            owner_root = workspace_context.roots.get(RootKind.OWNER_WRITABLE).canonical_path
            cache_parents = (
                (owner_root / "images",)
                if category == "image"
                else (owner_root / "cache" / "videos",)
            )
            if source_path.is_absolute() and source_path.parent in cache_parents:
                workspace_context.roots.remove(
                    RootKind.OWNER_WRITABLE,
                    source_path.relative_to(owner_root).as_posix(),
                )
        return result


def _dispatch_owner_tool(
    tool_name: str,
    arguments: dict[str, Any],
    invocation: ExecutorInvocation,
    skill_dir_materializer: Callable[[Any], str] | None,
    workspace_context: Any | None = None,
) -> str:
    if tool_name in _OWNER_MEDIA_TOOL_NAMES:
        return _dispatch_owner_media_tool(tool_name, arguments, workspace_context)

    if tool_name in OWNER_FILE_TOOL_NAMES:
        if workspace_context is None:
            raise OwnerToolRelayError("owner file relay workspace is unavailable")
        from tools import file_tools
        from tui_gateway.server import OwnerWorkerGatewayRuntime, owner_worker_gateway_runtime

        from hermes_cli.authenticated_file_context import AuthenticatedWorkspaceContext

        invocation_context = AuthenticatedWorkspaceContext(
            workspace_context.roots,
            workspace_prefix=invocation.identity.workspace_prefix,
            readonly_prefixes=invocation.identity.knowledge_prefixes,
        )
        runtime = OwnerWorkerGatewayRuntime(
            owner_key=invocation.identity.owner_key,
            worker_generation=invocation.identity.worker_generation,
            worker_id=invocation.identity.worker_id,
            lease_version=invocation.identity.lease_version,
            recovery_generation=invocation.identity.recovery_generation,
            filesystem_context=invocation_context,
        )
        with owner_worker_gateway_runtime(runtime):
            entry = file_tools.registry.get_entry(tool_name)
            if entry is None or entry.toolset != "file":
                raise OwnerToolRelayError("owner tool relay operation is not allowed")
            return str(entry.handler(
                dict(arguments),
                task_id=invocation.identity.task_id,
                session_id=invocation.identity.session_id,
                executor_identity=invocation.identity,
                executor_invocation=invocation,
            ))

    if tool_name in {"web_search", "web_extract"}:
        from hermes_cli.config import reload_env
        from tools.web_tools import web_extract_tool, web_search_tool

        # Provider implementations read their API keys from os.environ. Refresh
        # only from this worker's owner-scoped HERMES_HOME; the executor never
        # sees the resulting credentials.
        reload_env()
        if tool_name == "web_search":
            return str(web_search_tool(arguments["query"], limit=arguments["limit"]))
        return str(asyncio.run(web_extract_tool(
            arguments["urls"],
            "markdown",
            char_limit=arguments.get("char_limit"),
        )))

    from tools import skills_tool
    from tools.registry import registry

    # Importing the module registers the built-in handlers.
    del skills_tool
    entry = registry.get_entry(tool_name)
    if (
        entry is None
        or entry.toolset != "skills"
        or tool_name not in {"skills_list", "skill_view"}
    ):
        raise OwnerToolRelayError("owner tool relay operation is not allowed")
    result = entry.handler(
        dict(arguments),
        task_id=invocation.identity.task_id,
        session_id=invocation.identity.session_id,
        executor_identity=invocation.identity,
        executor_invocation=invocation,
        skill_dir_materializer=(
            skill_dir_materializer if tool_name == "skill_view" else None
        ),
    )
    return str(result)


@dataclass
class _RelayEndpoint:
    invocation: ExecutorInvocation
    connection: socket.socket
    thread: threading.Thread
    skill_dir_materializer: Callable[[Any], str] | None = None


class OwnerToolRelayBroker:
    """Owner-worker broker for exact one-shot owner tool invocations."""

    def __init__(
        self,
        *,
        identity_validator: Callable[[ExecutorIdentity], None],
        dispatcher: Callable[..., str] = _dispatch_owner_tool,
        media_dispatcher: Callable[..., str] | None = None,
        workspace_context: Any | None = None,
    ) -> None:
        self._identity_validator = identity_validator
        self._dispatcher = dispatcher
        self._media_dispatcher = media_dispatcher
        self._workspace_context = workspace_context
        self._endpoints: dict[tuple[tuple[Any, ...], str], _RelayEndpoint] = {}
        self._lock = threading.RLock()
        self._closed = False

    @staticmethod
    def _key(invocation: ExecutorInvocation) -> tuple[tuple[Any, ...], str]:
        return invocation.identity.stable_key, invocation.invocation_id

    def register(
        self,
        invocation: ExecutorInvocation,
        *,
        skill_dir_materializer: Callable[[Any], str] | None = None,
    ) -> int:
        """Return a child descriptor bound to this exact invocation."""
        if invocation.tool_name not in OWNER_RELAY_TOOL_NAMES:
            raise OwnerToolRelayError("owner tool relay operation is not allowed")
        if invocation.egress_profile is not EgressProfile.TOOL_NONE:
            raise OwnerToolRelayError("owner tool relay requires isolated network egress")
        self._identity_validator(invocation.identity)
        _validated_arguments(invocation.tool_name, invocation.arguments)
        if invocation.tool_name == "skill_view" and skill_dir_materializer is None:
            raise OwnerToolRelayError("owner tool relay skill materializer is unavailable")
        if invocation.tool_name != "skill_view" and skill_dir_materializer is not None:
            raise OwnerToolRelayError("owner tool relay skill materializer is unexpected")

        parent, child = socket.socketpair()
        parent.set_inheritable(False)
        child.set_inheritable(False)
        key = self._key(invocation)
        thread = threading.Thread(
            target=self._serve,
            args=(key,),
            daemon=True,
            name=f"owner-tool-relay-{invocation.invocation_id[:12]}",
        )
        endpoint = _RelayEndpoint(invocation, parent, thread, skill_dir_materializer)
        with self._lock:
            if self._closed or key in self._endpoints:
                parent.close()
                child.close()
                raise OwnerToolRelayError("owner tool relay is unavailable")
            self._endpoints[key] = endpoint
        thread.start()
        return child.detach()

    def _serve(self, key: tuple[tuple[Any, ...], str]) -> None:
        with self._lock:
            endpoint = self._endpoints.get(key)
        if endpoint is None:
            return
        try:
            request = _recv_frame(endpoint.connection, limit=_MAX_REQUEST_BYTES)
            result = self._handle_request(
                endpoint.invocation,
                request,
                endpoint.skill_dir_materializer,
            )
            _send_frame(
                endpoint.connection,
                {"ok": True, "result": result},
                limit=_MAX_RESPONSE_BYTES,
            )
        except (OSError, OwnerToolRelayError):
            try:
                _send_frame(
                    endpoint.connection,
                    {"ok": False, "error": "authenticated tool relay rejected the request"},
                    limit=_MAX_RESPONSE_BYTES,
                )
            except (OSError, OwnerToolRelayError):
                pass
        except Exception:
            try:
                _send_frame(
                    endpoint.connection,
                    {"ok": False, "error": "authenticated tool execution failed"},
                    limit=_MAX_RESPONSE_BYTES,
                )
            except (OSError, OwnerToolRelayError):
                pass
        finally:
            with self._lock:
                if self._endpoints.get(key) is endpoint:
                    self._endpoints.pop(key, None)
            endpoint.connection.close()

    def _handle_request(
        self,
        expected: ExecutorInvocation,
        request: dict[str, Any],
        skill_dir_materializer: Callable[[Any], str] | None,
    ) -> str:
        if set(request) != {"identity", "invocation_id", "tool_name", "arguments"}:
            raise OwnerToolRelayError("owner tool relay request is invalid")
        try:
            identity = ExecutorIdentity.from_payload(request["identity"])
        except Exception as exc:
            raise OwnerToolRelayError("owner tool relay identity is invalid") from exc
        if identity != expected.identity:
            raise OwnerToolRelayError("owner tool relay identity does not match invocation")
        self._identity_validator(identity)
        if request["invocation_id"] != expected.invocation_id:
            raise OwnerToolRelayError("owner tool relay invocation does not match")
        if request["tool_name"] != expected.tool_name:
            raise OwnerToolRelayError("owner tool relay operation does not match")
        arguments = _validated_arguments(expected.tool_name, request["arguments"])
        if arguments != _validated_arguments(expected.tool_name, expected.arguments):
            raise OwnerToolRelayError("owner tool relay arguments do not match")
        correlation = {
            "tool_name": expected.tool_name,
            "invocation_id": expected.invocation_id,
            "tool_call_id": expected.tool_call_id,
            "api_request_id": expected.api_request_id,
        }
        logger.info("Authenticated owner relay dispatch started", extra=correlation)
        try:
            use_media_dispatcher = (
                self._media_dispatcher is not None
                and expected.tool_name in _DEPLOYMENT_CAPABLE_MEDIA_TOOL_NAMES
            )
            dispatcher = self._media_dispatcher if use_media_dispatcher else self._dispatcher
            dispatcher_args = (
                (expected.tool_name, arguments, expected, skill_dir_materializer)
                if use_media_dispatcher
                else (expected.tool_name, arguments, expected, skill_dir_materializer, self._workspace_context)
                if expected.tool_name in OWNER_FILE_TOOL_NAMES | _OWNER_MEDIA_TOOL_NAMES
                else (expected.tool_name, arguments, expected, skill_dir_materializer)
            )
            result = dispatcher(*dispatcher_args)
            if not isinstance(result, str):
                raise OwnerToolRelayError("owner tool relay result is invalid")
            if len(result.encode("utf-8")) > expected.resource_decision.quota.output_bytes:
                raise OwnerToolRelayError("owner tool relay result exceeds executor quota")
        except Exception:
            logger.warning("Authenticated owner relay dispatch failed", extra=correlation)
            raise
        logger.info("Authenticated owner relay dispatch completed", extra=correlation)
        return result

    def revoke_invocation(self, invocation: ExecutorInvocation) -> int:
        return self._revoke(lambda endpoint: self._key(endpoint.invocation) == self._key(invocation))

    def revoke_executor(self, identity: ExecutorIdentity) -> int:
        return self._revoke(lambda endpoint: endpoint.invocation.identity == identity)

    def revoke_worker_generation(self, *, owner_key: str, worker_id: str, worker_generation: int) -> int:
        return self._revoke(
            lambda endpoint: (
                endpoint.invocation.identity.owner_key == owner_key
                and endpoint.invocation.identity.worker_id == worker_id
                and endpoint.invocation.identity.worker_generation == worker_generation
            )
        )

    def close(self) -> int:
        with self._lock:
            self._closed = True
        return self._revoke(lambda _endpoint: True)

    def _revoke(self, predicate: Callable[[_RelayEndpoint], bool]) -> int:
        with self._lock:
            selected = [
                (key, endpoint)
                for key, endpoint in self._endpoints.items()
                if predicate(endpoint)
            ]
            for key, _endpoint in selected:
                self._endpoints.pop(key, None)
        for _key, endpoint in selected:
            try:
                endpoint.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            endpoint.connection.close()
        return len(selected)


def dispatch_owner_tool_over_relay(inherited_fd: int, invocation: ExecutorInvocation) -> str:
    """Execute one admitted owner invocation through its inherited relay FD."""
    if invocation.tool_name not in OWNER_RELAY_TOOL_NAMES:
        raise OwnerToolRelayError("owner tool relay operation is not allowed")
    if inherited_fd < 0:
        raise OwnerToolRelayError("owner tool relay descriptor is invalid")
    connection = socket.socket(fileno=inherited_fd)
    connection.set_inheritable(False)
    try:
        _send_frame(
            connection,
            {
                "identity": invocation.identity.to_payload(),
                "invocation_id": invocation.invocation_id,
                "tool_name": invocation.tool_name,
                "arguments": dict(invocation.arguments),
            },
            limit=_MAX_REQUEST_BYTES,
        )
        response = _recv_frame(connection, limit=_MAX_RESPONSE_BYTES)
        if set(response) == {"ok", "result"} and response["ok"] is True and isinstance(response["result"], str):
            return response["result"]
        raise OwnerToolRelayError("authenticated owner tool relay rejected the request")
    except OSError as exc:
        raise OwnerToolRelayError("authenticated owner tool relay is unavailable") from exc
    finally:
        connection.close()
