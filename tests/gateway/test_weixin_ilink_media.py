"""Security and protocol tests for shared iLink media downloads."""

from __future__ import annotations

import asyncio
import base64
import socket

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from gateway.weixin_ilink.media import (
    PublicAddressResolver,
    WeixinMediaError,
    WeixinMediaLimits,
    cdn_download_url,
    decrypt_aes128_ecb,
    download_and_decrypt_voice,
    parse_aes_key,
    validate_media_url,
)


class _Content:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self.chunks:
            yield chunk


class _Response:
    def __init__(self, chunks: list[bytes], *, status: int = 200, headers=None) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = _Content(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response
        self.calls = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _encrypt(plaintext: bytes, key: bytes) -> bytes:
    pad = 16 - len(plaintext) % 16
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(plaintext + bytes([pad]) * pad) + encryptor.finalize()


def test_cdn_url_encodes_signed_query_as_one_parameter():
    assert cdn_download_url("https://novac2c.cdn.weixin.qq.com/c2c", "a&b=/+") == (
        "https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param=a%26b%3D%2F%2B"
    )


def test_aes_key_and_padding_are_strictly_validated():
    key = b"k" * 16
    encoded_hex_key = base64.b64encode(key.hex().encode("ascii")).decode("ascii")
    assert parse_aes_key(encoded_hex_key) == key
    assert decrypt_aes128_ecb(_encrypt(b"voice", key), key) == b"voice"
    with pytest.raises(WeixinMediaError, match="invalid_aes_key"):
        parse_aes_key("not-base64")
    with pytest.raises(WeixinMediaError, match="invalid_ciphertext"):
        decrypt_aes128_ecb(b"short", key)
    with pytest.raises(WeixinMediaError, match="invalid_padding"):
        decrypt_aes128_ecb(b"0" * 16, key)


@pytest.mark.parametrize(
    "url",
    [
        "http://novac2c.cdn.weixin.qq.com/file",
        "https://user@novac2c.cdn.weixin.qq.com/file",
        "https://novac2c.cdn.weixin.qq.com:444/file",
        "https://novac2c.cdn.weixin.qq.com/file#fragment",
        "https://127.0.0.1/file",
        "https://example.com/file",
    ],
)
def test_media_url_rejects_unsafe_targets(url):
    with pytest.raises(WeixinMediaError, match="unsafe_media_url"):
        validate_media_url(url)


@pytest.mark.asyncio
async def test_download_decrypts_without_redirects():
    key = b"k" * 16
    session = _Session(_Response([_encrypt(b"#!SILK_V3 test", key)]))

    result = await download_and_decrypt_voice(
        session,
        descriptor={
            "v": 1,
            "media": {
                "encrypt_query_param": "signed",
                "aes_key": base64.b64encode(key).decode("ascii"),
            },
        },
    )

    assert result == b"#!SILK_V3 test"
    assert session.calls[0][1] == {"allow_redirects": False}


@pytest.mark.asyncio
async def test_download_rejects_redirect_and_streamed_oversize():
    descriptor = {"v": 1, "media": {"full_url": "https://novac2c.cdn.weixin.qq.com/file"}}
    with pytest.raises(WeixinMediaError, match="media_redirected"):
        await download_and_decrypt_voice(_Session(_Response([], status=302)), descriptor=descriptor)

    session = _Session(_Response([b"123", b"456"]))
    with pytest.raises(WeixinMediaError, match="media_too_large"):
        await download_and_decrypt_voice(
            session,
            descriptor=descriptor,
            limits=WeixinMediaLimits(max_download_bytes=5),
        )


@pytest.mark.asyncio
async def test_public_resolver_rejects_private_addresses_and_pins_results(monkeypatch):
    loop = asyncio.get_running_loop()

    async def private(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]

    monkeypatch.setattr(loop, "getaddrinfo", private)
    with pytest.raises(WeixinMediaError, match="unsafe_media_address"):
        await PublicAddressResolver().resolve("novac2c.cdn.weixin.qq.com", 443)

    async def public(*_args, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(loop, "getaddrinfo", public)
    records = await PublicAddressResolver().resolve("novac2c.cdn.weixin.qq.com", 443)
    assert records == [{
        "hostname": "novac2c.cdn.weixin.qq.com",
        "host": "8.8.8.8",
        "port": 443,
        "family": socket.AF_INET,
        "proto": 6,
        "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
    }]
