"""Bounded inbound file downloads for the central iLink connector."""

from __future__ import annotations

import base64
import os
import re
import secrets
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlparse

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
MAX_INBOUND_FILE_BYTES = 32 * 1024 * 1024
_ALLOWED_CDN_HOSTS = frozenset(
    {
        "novac2c.cdn.weixin.qq.com",
        "ilinkai.weixin.qq.com",
        "res.wx.qq.com",
        "mmbiz.qpic.cn",
    }
)
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._\--￿]+")


def extract_file_descriptors(items: Any) -> tuple[str, list[dict[str, str]]] | None:
    """Return supported text/file content, or ``None`` for unsupported input."""
    if not isinstance(items, list) or not items:
        return None
    text = ""
    files: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, Mapping):
            return None
        item_type = item.get("type")
        if item_type == 1:
            value = (item.get("text_item") or {}).get("text")
            if isinstance(value, str) and value.strip() and not text:
                text = value
            continue
        if item_type != 4:
            return None
        file_item = item.get("file_item") or {}
        media = file_item.get("media") or {}
        encrypted_query_param = str(media.get("encrypt_query_param") or "").strip()
        full_url = str(media.get("full_url") or "").strip()
        aes_key = str(media.get("aes_key") or "").strip()
        if not aes_key or not (encrypted_query_param or full_url):
            return None
        raw_size = str(file_item.get("len") or file_item.get("file_size") or "").strip()
        if raw_size:
            try:
                if int(raw_size) > MAX_INBOUND_FILE_BYTES:
                    return None
            except ValueError:
                return None
        files.append(
            {
                "file_name": sanitize_filename(str(file_item.get("file_name") or "document.bin")),
                "encrypt_query_param": encrypted_query_param,
                "full_url": full_url,
                "aes_key": aes_key,
            }
        )
    if not text and not files:
        return None
    return text, files


async def download_file(
    session: Any,
    descriptor: Mapping[str, str],
    *,
    destination: Path,
) -> Path:
    """Download, decrypt, and atomically stage one iLink file."""
    encrypted_query_param = str(descriptor.get("encrypt_query_param") or "").strip()
    full_url = str(descriptor.get("full_url") or "").strip()
    if encrypted_query_param:
        url = (
            f"{WEIXIN_CDN_BASE_URL}/download?encrypted_query_param="
            f"{quote(encrypted_query_param, safe='')}"
        )
    elif full_url:
        url = full_url
    else:
        raise ValueError("iLink file has no download reference")
    _assert_cdn_url(url)

    async with session.get(url) as response:
        if not getattr(response, "ok", 200 <= int(response.status) < 300):
            raise RuntimeError(f"iLink file download failed with HTTP {response.status}")
        encrypted = await _read_bounded(response, MAX_INBOUND_FILE_BYTES + 16)
    plaintext = _aes128_ecb_decrypt(encrypted, _parse_aes_key(descriptor["aes_key"]))
    if len(plaintext) > MAX_INBOUND_FILE_BYTES:
        raise ValueError("iLink file exceeds the inbound size limit")

    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(8)}.tmp")
    descriptor_fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor_fd, "wb") as handle:
            handle.write(plaintext)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


async def _read_bounded(response: Any, limit: int) -> bytes:
    content_length = (getattr(response, "headers", None) or {}).get("Content-Length")
    if content_length:
        try:
            declared_size = int(content_length)
        except (TypeError, ValueError):
            declared_size = None
        if declared_size is not None and declared_size > limit:
            raise ValueError("iLink encrypted file exceeds the inbound size limit")
    content = getattr(response, "content", None)
    if content is not None and hasattr(content, "iter_chunked"):
        chunks: list[bytes] = []
        total = 0
        async for chunk in content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > limit:
                raise ValueError("iLink encrypted file exceeds the inbound size limit")
            chunks.append(chunk)
        return b"".join(chunks)
    data = await response.read()
    if len(data) > limit:
        raise ValueError("iLink encrypted file exceeds the inbound size limit")
    return data


def sanitize_filename(value: str) -> str:
    name = Path(value.strip()).name.replace("`", "_")
    name = _SAFE_FILENAME_RE.sub("_", name).strip(". ")
    return name[:200] or "document.bin"


def _assert_cdn_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in _ALLOWED_CDN_HOSTS:
        raise ValueError("iLink file URL is outside the trusted WeChat CDN")


def _parse_aes_key(value: str) -> bytes:
    decoded = base64.b64decode(value, validate=True)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        try:
            return bytes.fromhex(decoded.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            pass
    raise ValueError("iLink file has an invalid AES key")


def _aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        return padded
    padding = padded[-1]
    if not 1 <= padding <= 16 or not padded.endswith(bytes([padding]) * padding):
        raise ValueError("iLink file has invalid encrypted padding")
    return padded[:-padding]
