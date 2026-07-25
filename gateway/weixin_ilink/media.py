"""Bounded download and decryption for Tencent iLink media."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

from aiohttp.abc import AbstractResolver, ResolveResult

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
WEIXIN_MEDIA_HOSTS = frozenset({
    "novac2c.cdn.weixin.qq.com",
    "ilinkai.weixin.qq.com",
})


class WeixinMediaError(RuntimeError):
    """Sanitized iLink media failure with stable retry metadata."""

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        self.code = code
        self.retryable = retryable
        super().__init__(code)


@dataclass(frozen=True)
class WeixinMediaLimits:
    max_download_bytes: int = 6 * 1024 * 1024
    timeout_seconds: float = 60.0


def cdn_download_url(cdn_base_url: str, encrypted_query_param: str) -> str:
    return (
        f"{cdn_base_url.rstrip('/')}/download?encrypted_query_param="
        f"{quote(encrypted_query_param, safe='')}"
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
                host,
                port,
                type=socket.SOCK_STREAM,
                family=family,
                flags=socket.AI_ADDRCONFIG,
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


async def _download_bytes(
    session: Any,
    url: str,
    *,
    limits: WeixinMediaLimits,
) -> bytes:
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


async def download_and_decrypt_voice(
    session: Any,
    *,
    descriptor: dict[str, Any],
    cdn_base_url: str = WEIXIN_CDN_BASE_URL,
    limits: WeixinMediaLimits = WeixinMediaLimits(),
) -> bytes:
    media = descriptor.get("media")
    if descriptor.get("v") != 1 or not isinstance(media, dict):
        raise WeixinMediaError("invalid_media_descriptor")
    encrypted_query = media.get("encrypt_query_param")
    full_url = media.get("full_url")
    aes_key = media.get("aes_key")
    if encrypted_query:
        if not isinstance(encrypted_query, str):
            raise WeixinMediaError("invalid_media_descriptor")
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
