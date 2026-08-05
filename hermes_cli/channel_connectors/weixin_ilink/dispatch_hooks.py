"""iLink media and outbound hooks for the provider-neutral dispatcher."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import stat
from pathlib import Path
from typing import Mapping

from gateway.platforms.response_media import (
    extract_images,
    extract_local_files,
    extract_media,
)
from gateway.weixin_ilink.media import (
    WeixinMediaError,
    WeixinMediaLimits,
    download_and_decrypt_media,
    sanitize_filename,
)
from hermes_cli.channel_connectors.contracts import MediaMaterializationRequest
from hermes_cli.channel_dispatch.dispatcher import MediaDispatchError


def _admit_attachment_root(workspace_root: Path, inbound_id: str) -> Path:
    if not inbound_id or not all(character.isascii() and (character.isalnum() or character in "_-") for character in inbound_id):
        raise MediaDispatchError("media_staging_unsafe", retryable=False)
    workspace = workspace_root.resolve(strict=True)
    if workspace_root.is_symlink() or not workspace.is_dir():
        raise MediaDispatchError("media_staging_unsafe", retryable=False)
    current = workspace
    for component in (".hermes", "channel-attachments", "weixin_ilink", inbound_id):
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise MediaDispatchError("media_staging_unsafe", retryable=False)
        if not current.resolve(strict=True).is_relative_to(workspace):
            raise MediaDispatchError("media_staging_unsafe", retryable=False)
    return current


def _stage_contained_media(
    data: bytes,
    *,
    workspace_root: Path,
    destination: Path,
) -> Path:
    root = workspace_root.resolve(strict=True)
    parent = destination.parent.resolve(strict=True)
    if not parent.is_relative_to(root) or destination.exists() or destination.is_symlink():
        raise MediaDispatchError("media_staging_unsafe", retryable=False)
    temporary = parent / f".{destination.name}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if parent.resolve(strict=True) != parent or not parent.is_relative_to(root):
            raise MediaDispatchError("media_staging_unsafe", retryable=False)
        os.replace(temporary, destination)
        resolved = destination.resolve(strict=True)
        if not resolved.is_relative_to(root) or destination.is_symlink():
            destination.unlink(missing_ok=True)
            raise MediaDispatchError("media_staging_unsafe", retryable=False)
        return resolved
    finally:
        temporary.unlink(missing_ok=True)


class WeixinDispatchHooks:
    def __init__(self, session, *, config: dict | None = None) -> None:
        self.session = session
        config = config or {}
        self.voice_enabled = bool(config.get("voice_enabled", True))
        self.voice_limits = WeixinMediaLimits(
            max_download_bytes=int(config.get("voice_max_download_bytes", 6 * 1024 * 1024)),
            timeout_seconds=float(config.get("voice_download_timeout_seconds", 60)),
        )
        self.media_limits = WeixinMediaLimits(
            max_download_bytes=int(config.get("media_max_download_bytes", 32 * 1024 * 1024)),
            timeout_seconds=float(config.get("media_download_timeout_seconds", 120)),
        )
        self.voice_max_duration = float(config.get("voice_max_duration_seconds", 300))
        self.voice_stt_timeout = float(config.get("voice_stt_timeout_seconds", 600))
        self.voice_temp_ttl = int(config.get("voice_temp_ttl_seconds", 3600))

    async def materialize(self, request: MediaMaterializationRequest) -> str:
        if self.session is None:
            raise MediaDispatchError("media_disabled", retryable=False)
        if request.payload_kind == "voice_media":
            return await self._transcribe_voice(request)
        return await self._attach_media(request)

    async def _attach_media(self, request: MediaMaterializationRequest) -> str:
        workspace_root = request.owner.owner_home / "workspaces" / "default"
        attachment_root = _admit_attachment_root(
            workspace_root,
            str(request.claim["inbound_id"]),
        )
        references: list[str] = []
        for index, descriptor in enumerate(request.attachments, start=1):
            if not isinstance(descriptor, Mapping) or descriptor.get("kind") not in {
                "image", "video", "file"
            }:
                raise MediaDispatchError("media_descriptor_invalid", retryable=False)
            name = sanitize_filename(descriptor.get("file_name"), default="document.bin")
            try:
                data = await download_and_decrypt_media(
                    self.session,
                    descriptor={"v": 1, "media": descriptor.get("media")},
                    limits=self.media_limits,
                )
            except WeixinMediaError as exc:
                raise MediaDispatchError(exc.code, retryable=exc.retryable) from exc
            destination = _stage_contained_media(
                data,
                workspace_root=workspace_root,
                destination=attachment_root / f"{index}-{name}",
            )
            try:
                if descriptor["kind"] == "image":
                    result = await request.client.call(
                        "image.attach",
                        {"session_id": request.session_id, "path": str(destination)},
                    )
                    reference = str((result or {}).get("text") or f"[User attached image: {name}]")
                else:
                    result = await request.client.call(
                        "file.attach",
                        {"session_id": request.session_id, "path": str(destination), "name": name},
                    )
                    reference = str((result or {}).get("ref_text") or "")
            except Exception as exc:
                raise MediaDispatchError("owner_worker_unavailable", retryable=True) from exc
            if not reference:
                raise MediaDispatchError("media_attach_failed", retryable=True)
            references.append(reference)
        return "\n\n".join(part for part in [request.text.strip(), *references] if part)

    async def _transcribe_voice(self, request: MediaMaterializationRequest) -> str:
        if not self.voice_enabled:
            raise MediaDispatchError("voice_disabled", retryable=False)
        try:
            descriptor = json.loads(request.text)
            if not isinstance(descriptor, dict):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MediaDispatchError("voice_media_invalid", retryable=False) from exc
        playtime = descriptor.get("playtime")
        if isinstance(playtime, int) and playtime > self.voice_max_duration * 1000:
            raise MediaDispatchError("voice_too_long", retryable=False)
        try:
            media = await download_and_decrypt_media(
                self.session,
                descriptor=descriptor,
                limits=self.voice_limits,
            )
        except WeixinMediaError as exc:
            raise MediaDispatchError(exc.code, retryable=exc.retryable) from exc
        request_key = f"weixin_ilink:{request.claim['inbound_id']}"
        finished = False
        try:
            await request.client.call(
                "channel.voice.begin",
                {
                    "session_id": request.session_id,
                    "request_key": request_key,
                    "size": len(media),
                    "sha256": hashlib.sha256(media).hexdigest(),
                    "temp_ttl_seconds": self.voice_temp_ttl,
                },
            )
            offset = 0
            for start in range(0, len(media), 256 * 1024):
                chunk = media[start:start + 256 * 1024]
                result = await request.client.call(
                    "channel.voice.chunk",
                    {
                        "session_id": request.session_id,
                        "request_key": request_key,
                        "offset": offset,
                        "data": base64.b64encode(chunk).decode("ascii"),
                    },
                )
                offset = int(result.get("offset", -1))
                if offset != start + len(chunk):
                    raise MediaDispatchError("voice_upload_failed", retryable=True)
            result = await asyncio.wait_for(
                request.client.call(
                    "channel.voice.finish",
                    {
                        "session_id": request.session_id,
                        "request_key": request_key,
                        "timeout_seconds": self.voice_stt_timeout,
                        "max_duration_seconds": self.voice_max_duration,
                    },
                ),
                timeout=self.voice_stt_timeout + 30,
            )
            finished = True
        except MediaDispatchError:
            raise
        except Exception as exc:
            raise MediaDispatchError("owner_worker_unavailable", retryable=True) from exc
        finally:
            if not finished:
                try:
                    await request.client.call(
                        "channel.voice.abort",
                        {"session_id": request.session_id, "request_key": request_key},
                    )
                except Exception:
                    pass
        if not result.get("success"):
            raise MediaDispatchError(
                str(result.get("code") or "stt_failed"),
                retryable=bool(result.get("retryable")),
            )
        transcript = str(result.get("transcript") or "").strip()
        if not transcript:
            raise MediaDispatchError("stt_empty", retryable=False)
        return transcript


def encode_weixin_outbound(response_text: str) -> str:
    media, cleaned = extract_media(response_text)
    remote_images, cleaned = extract_images(cleaned)
    local_files, cleaned = extract_local_files(cleaned)
    if remote_images:
        retained = "\n".join(
            f"![{alt}]({url})" if alt else url
            for url, alt in remote_images
        )
        cleaned = "\n".join(part for part in (cleaned, retained) if part)
    attachments: list[dict[str, object]] = []
    seen: set[str] = set()
    for path, is_voice in [*media, *((path, False) for path in local_files)]:
        raw = str(path)
        if raw in seen:
            continue
        seen.add(raw)
        attachments.append({"path": raw, "voice": bool(is_voice)})
    return json.dumps(
        {
            "v": 1,
            "text": cleaned if attachments else response_text,
            "attachments": attachments,
            "metadata": {"provider": "weixin_ilink"},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
