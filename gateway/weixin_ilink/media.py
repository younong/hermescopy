"""Bounded media transfer for Tencent iLink messages."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import mimetypes
import os
import re
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlparse

from aiohttp.abc import AbstractResolver, ResolveResult
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
WEIXIN_MEDIA_HOSTS = frozenset({
    "novac2c.cdn.weixin.qq.com",
    "ilinkai.weixin.qq.com",
})

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_FILE = 3
MEDIA_VOICE = 4

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._\--￿]+")


class WeixinMediaError(RuntimeError):
    """Sanitized iLink media failure with stable retry metadata."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True)
class WeixinMediaLimits:
    max_download_bytes: int = 32 * 1024 * 1024
    timeout_seconds: float = 60.0


def sanitize_filename(value: str, *, default: str = "document.bin") -> str:
    name = Path(str(value or "").strip()).name.replace("`", "_")
    name = _SAFE_FILENAME_RE.sub("_", name).strip(". ")
    return name[:200] or default


def cdn_download_url(cdn_base_url: str, encrypted_query_param: str) -> str:
    return (
        f"{cdn_base_url.rstrip('/')}/download?encrypted_query_param="
        f"{quote(encrypted_query_param, safe='')}"
    )


def cdn_upload_url(cdn_base_url: str, upload_param: str, filekey: str) -> str:
    return (
        f"{cdn_base_url.rstrip('/')}/upload"
        f"?encrypted_query_param={quote(upload_param, safe='')}"
        f"&filekey={quote(filekey, safe='')}"
    )


def parse_aes_key(aes_key_b64: str) -> bytes:
    try:
        decoded = base64.b64decode(aes_key_b64, validate=True)
    except (TypeError, ValueError) as exc:
        raise WeixinMediaError("invalid_aes_key") from exc
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        try:
            text = decoded.decode("ascii")
            if all(ch in "0123456789abcdefABCDEF" for ch in text):
                return bytes.fromhex(text)
        except (UnicodeDecodeError, ValueError):
            pass
    raise WeixinMediaError("invalid_aes_key")


def encrypt_aes128_ecb(plaintext: bytes, key: bytes) -> bytes:
    pad_len = 16 - len(plaintext) % 16
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    encryptor = cipher.encryptor()
    return encryptor.update(plaintext + bytes([pad_len]) * pad_len) + encryptor.finalize()


def decrypt_aes128_ecb(ciphertext: bytes, key: bytes) -> bytes:
    if not ciphertext or len(ciphertext) % 16:
        raise WeixinMediaError("invalid_ciphertext")
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    pad_len = padded[-1]
    if not 1 <= pad_len <= 16 or not padded.endswith(bytes([pad_len]) * pad_len):
        raise WeixinMediaError("invalid_padding")
    return padded[:-pad_len]


def aes_padded_size(size: int) -> int:
    return ((size + 1 + 15) // 16) * 16


def validate_media_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise WeixinMediaError("unsafe_media_url") from exc
    if (
        parsed.scheme.lower() != "https"
        or host not in WEIXIN_MEDIA_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in {None, 443}
    ):
        raise WeixinMediaError("unsafe_media_url")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise WeixinMediaError("unsafe_media_url")


class PublicAddressResolver(AbstractResolver):
    """Resolve once and return only public addresses to aiohttp."""

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        loop = asyncio.get_running_loop()
        try:
            records = await loop.getaddrinfo(
                host, port, type=socket.SOCK_STREAM, family=family, flags=socket.AI_ADDRCONFIG
            )
        except OSError as exc:
            raise WeixinMediaError("media_dns_failed", retryable=True) from exc
        if not records:
            raise WeixinMediaError("media_dns_failed", retryable=True)
        resolved: list[ResolveResult] = []
        for address_family, _type, protocol, _canonname, sock_address in records:
            try:
                address = ipaddress.ip_address(sock_address[0])
            except ValueError as exc:
                raise WeixinMediaError("unsafe_media_address") from exc
            if not address.is_global:
                raise WeixinMediaError("unsafe_media_address")
            resolved.append(
                ResolveResult(
                    hostname=host,
                    host=str(address),
                    port=int(sock_address[1]),
                    family=address_family,
                    proto=protocol,
                    flags=socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
                )
            )
        return resolved

    async def close(self) -> None:
        return None


async def _download_bytes(session: Any, url: str, *, limits: WeixinMediaLimits) -> bytes:
    validate_media_url(url)

    async def _do_download() -> bytes:
        try:
            async with session.get(url, allow_redirects=False) as response:
                if 300 <= response.status < 400:
                    raise WeixinMediaError("media_redirected")
                if response.status == 429 or response.status >= 500:
                    raise WeixinMediaError("media_http_unavailable", retryable=True)
                if response.status != 200:
                    raise WeixinMediaError("media_http_rejected")
                raw_length = response.headers.get("Content-Length")
                if raw_length:
                    try:
                        content_length = int(raw_length)
                    except ValueError as exc:
                        raise WeixinMediaError("invalid_content_length") from exc
                    if content_length > limits.max_download_bytes:
                        raise WeixinMediaError("media_too_large")
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.content.iter_chunked(64 * 1024):
                    total += len(chunk)
                    if total > limits.max_download_bytes:
                        raise WeixinMediaError("media_too_large")
                    chunks.append(chunk)
                if not chunks:
                    raise WeixinMediaError("media_empty")
                return b"".join(chunks)
        except WeixinMediaError:
            raise
        except (asyncio.TimeoutError, OSError) as exc:
            raise WeixinMediaError("media_download_failed", retryable=True) from exc

    try:
        return await asyncio.wait_for(_do_download(), timeout=limits.timeout_seconds)
    except asyncio.TimeoutError as exc:
        raise WeixinMediaError("media_download_timeout", retryable=True) from exc


async def download_and_decrypt_media(
    session: Any,
    *,
    descriptor: Mapping[str, Any],
    cdn_base_url: str = WEIXIN_CDN_BASE_URL,
    limits: WeixinMediaLimits = WeixinMediaLimits(),
) -> bytes:
    media = descriptor.get("media")
    if descriptor.get("v") != 1 or not isinstance(media, Mapping):
        raise WeixinMediaError("invalid_media_descriptor")
    encrypted_query = media.get("encrypt_query_param")
    full_url = media.get("full_url")
    aes_key = media.get("aes_key")
    if isinstance(encrypted_query, str) and encrypted_query:
        url = cdn_download_url(cdn_base_url, encrypted_query)
    elif isinstance(full_url, str) and full_url:
        url = full_url
    else:
        raise WeixinMediaError("invalid_media_descriptor")
    raw = await _download_bytes(session, url, limits=limits)
    if aes_key is not None:
        if not isinstance(aes_key, str):
            raise WeixinMediaError("invalid_media_descriptor")
        raw = decrypt_aes128_ecb(raw, parse_aes_key(aes_key))
    if not raw or len(raw) > limits.max_download_bytes:
        raise WeixinMediaError("media_too_large" if raw else "media_empty")
    return raw


def stage_media_file(data: bytes, destination: Path) -> Path:
    """Atomically stage media with owner-only permissions."""
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.tmp")
    descriptor_fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        with os.fdopen(descriptor_fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def media_kind_for_path(path: str, *, force_file: bool = False) -> tuple[str, int, int]:
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if not force_file and mime.startswith("image/"):
        return "image", ITEM_IMAGE, MEDIA_IMAGE
    if not force_file and mime.startswith("video/"):
        return "video", ITEM_VIDEO, MEDIA_VIDEO
    if not force_file and path.lower().endswith(".silk"):
        return "voice", ITEM_VOICE, MEDIA_VOICE
    return "file", ITEM_FILE, MEDIA_FILE


def build_media_item(
    *,
    kind: str,
    encrypted_query_param: str,
    aes_key_for_api: str,
    ciphertext_size: int,
    plaintext_size: int,
    filename: str,
    rawfilemd5: str,
) -> dict[str, Any]:
    media = {
        "encrypt_query_param": encrypted_query_param,
        "aes_key": aes_key_for_api,
        "encrypt_type": 1,
    }
    if kind == "image":
        return {"type": ITEM_IMAGE, "image_item": {"media": media, "mid_size": ciphertext_size}}
    if kind == "video":
        return {
            "type": ITEM_VIDEO,
            "video_item": {
                "media": media,
                "video_size": ciphertext_size,
                "play_length": 0,
                "video_md5": rawfilemd5,
            },
        }
    if kind == "voice":
        return {
            "type": ITEM_VOICE,
            "voice_item": {
                "media": media,
                "encode_type": 6,
                "bits_per_sample": 16,
                "sample_rate": 24000,
                "playtime": 0,
            },
        }
    return {
        "type": ITEM_FILE,
        "file_item": {
            "media": media,
            "file_name": filename,
            "len": str(plaintext_size),
        },
    }


async def upload_media_item(
    session: Any,
    client: Any,
    *,
    to_user_id: str,
    path: str,
    cdn_base_url: str = WEIXIN_CDN_BASE_URL,
    force_file: bool = False,
) -> dict[str, Any]:
    """Encrypt, upload, and construct one native iLink media item."""
    try:
        plaintext = Path(path).read_bytes()
    except OSError as exc:
        raise WeixinMediaError("media_file_unavailable") from exc
    kind, _item_type, media_type = media_kind_for_path(path, force_file=force_file)
    filekey = secrets.token_hex(16)
    aes_key = secrets.token_bytes(16)
    rawfilemd5 = hashlib.md5(plaintext).hexdigest()
    upload_response = await client.post_json(
        endpoint="ilink/bot/getuploadurl",
        payload={
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": len(plaintext),
            "rawfilemd5": rawfilemd5,
            "filesize": aes_padded_size(len(plaintext)),
            "no_need_thumb": True,
            "aeskey": aes_key.hex(),
        },
        timeout_ms=15_000,
        operation="get upload URL",
    )
    upload_param = str(upload_response.get("upload_param") or "")
    upload_full_url = str(upload_response.get("upload_full_url") or "")
    if upload_full_url:
        validate_media_url(upload_full_url)
        upload_url = upload_full_url
    elif upload_param:
        upload_url = cdn_upload_url(cdn_base_url, upload_param, filekey)
    else:
        raise WeixinMediaError("media_upload_url_missing")
    ciphertext = encrypt_aes128_ecb(plaintext, aes_key)

    async def _upload() -> str:
        try:
            async with session.post(
                upload_url,
                data=ciphertext,
                headers={"Content-Type": "application/octet-stream"},
                allow_redirects=False,
            ) as response:
                if response.status == 429 or response.status >= 500:
                    raise WeixinMediaError("media_upload_unavailable", retryable=True)
                if response.status != 200:
                    raise WeixinMediaError("media_upload_rejected")
                encrypted_param = response.headers.get("x-encrypted-param")
                await response.read()
                if not encrypted_param:
                    raise WeixinMediaError("media_upload_invalid")
                return encrypted_param
        except WeixinMediaError:
            raise
        except (asyncio.TimeoutError, OSError) as exc:
            raise WeixinMediaError("media_upload_failed", retryable=True) from exc

    try:
        encrypted_query_param = await asyncio.wait_for(_upload(), timeout=120)
    except asyncio.TimeoutError as exc:
        raise WeixinMediaError("media_upload_timeout", retryable=True) from exc
    return build_media_item(
        kind=kind,
        encrypted_query_param=encrypted_query_param,
        aes_key_for_api=base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii"),
        ciphertext_size=len(ciphertext),
        plaintext_size=len(plaintext),
        filename=Path(path).name,
        rawfilemd5=rawfilemd5,
    )
