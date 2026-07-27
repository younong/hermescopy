"""Security and protocol tests for shared iLink media downloads."""

from __future__ import annotations

import asyncio
import base64
import os
import shutil
import socket
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from gateway.weixin_ilink.media import (
    PublicAddressResolver,
    WeixinMediaError,
    WeixinMediaLimits,
    WeixinVoiceLimits,
    build_media_item,
    cdn_download_url,
    decrypt_aes128_ecb,
    download_and_decrypt_media,
    parse_aes_key,
    prepare_weixin_voice,
    silk_playtime_ms,
    upload_media_item,
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


def _write_test_wav(path: Path, *, frames: int = 2400) -> None:
    import wave

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * frames)


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

    result = await download_and_decrypt_media(
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
        await download_and_decrypt_media(_Session(_Response([], status=302)), descriptor=descriptor)

    session = _Session(_Response([b"123", b"456"]))
    with pytest.raises(WeixinMediaError, match="media_too_large"):
        await download_and_decrypt_media(
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


def test_voice_item_requires_positive_playtime():
    values = {
        "kind": "voice",
        "encrypted_query_param": "signed",
        "aes_key_for_api": "key",
        "ciphertext_size": 32,
        "plaintext_size": 16,
        "filename": "voice.silk",
        "rawfilemd5": "md5",
    }
    with pytest.raises(WeixinMediaError, match="voice_playtime_invalid"):
        build_media_item(**values)

    item = build_media_item(**values, voice_playtime_ms=1234)
    assert item["type"] == 3
    assert item["voice_item"] == {
        "media": {
            "encrypt_query_param": "signed",
            "aes_key": "key",
            "encrypt_type": 1,
        },
        "encode_type": 6,
        "bits_per_sample": 16,
        "sample_rate": 24000,
        "playtime": 1234,
    }


def test_prepare_voice_rejects_unsafe_or_unsupported_input(tmp_path):
    source = tmp_path / "voice.mp3"
    source.write_bytes(b"audio")
    symlink = tmp_path / "linked.mp3"
    symlink.symlink_to(source)
    with pytest.raises(WeixinMediaError, match="media_file_unavailable"):
        with prepare_weixin_voice(str(symlink)):
            pass

    unsupported = tmp_path / "voice.flac"
    unsupported.write_bytes(b"audio")
    with pytest.raises(WeixinMediaError, match="voice_format_unsupported"):
        with prepare_weixin_voice(str(unsupported)):
            pass

    with pytest.raises(WeixinMediaError, match="voice_source_too_large"):
        with prepare_weixin_voice(
            str(source),
            limits=WeixinVoiceLimits(max_source_bytes=2),
        ):
            pass


def test_prepare_voice_cleans_temporary_files_on_failure(tmp_path, monkeypatch):
    from gateway.weixin_ilink import media

    source = tmp_path / "voice.wav"
    _write_test_wav(source)
    temporary_root = tmp_path / "voice-work"

    def make_temporary_root(**_kwargs):
        temporary_root.mkdir()
        return str(temporary_root)

    monkeypatch.setattr(media.tempfile, "mkdtemp", make_temporary_root)
    monkeypatch.setattr(media.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        media,
        "_run_media_command",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(WeixinMediaError("voice_encoding_failed")),
    )

    with pytest.raises(WeixinMediaError, match="voice_encoding_failed"):
        with prepare_weixin_voice(str(source)):
            pass
    assert not temporary_root.exists()


def test_prepare_voice_timeout_kills_process(monkeypatch):
    from gateway.weixin_ilink import media

    class Process:
        pid = 4321

        def poll(self):
            return None

        def wait(self, timeout):
            if timeout == 5:
                return -9
            raise subprocess.TimeoutExpired("ffmpeg", timeout)

    killed = []
    monkeypatch.setattr(media.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(media.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    with pytest.raises(WeixinMediaError, match="voice_encoding_timeout"):
        media._run_media_command(["ffmpeg"], timeout_seconds=1)
    if os.name != "nt":
        assert killed == [(4321, media.signal.SIGKILL)]


def test_silk_playtime_parser_rejects_malformed_packets():
    valid = b"\x02#!SILK_V3" + (3).to_bytes(2, "little", signed=True) + b"abc"
    assert silk_playtime_ms(valid) == 20
    with pytest.raises(WeixinMediaError, match="voice_silk_invalid"):
        silk_playtime_ms(b"not silk")
    with pytest.raises(WeixinMediaError, match="voice_silk_invalid"):
        silk_playtime_ms(b"\x02#!SILK_V3\x05\x00abc")


def test_real_silk_encoder_runs_with_fixed_tencent_parameters(tmp_path):
    pilk = pytest.importorskip("pilk")
    pcm = tmp_path / "voice.pcm"
    pcm.write_bytes(b"\x00\x00" * 2400)
    silk = tmp_path / "voice.silk"
    helper = Path(__file__).resolve().parents[2] / "tools" / "silk_encoder.py"

    result = subprocess.run(
        [os.sys.executable, "-I", str(helper), str(pcm), str(silk)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0
    assert silk.read_bytes().startswith(b"\x02#!SILK_V3")
    assert pilk.get_duration(str(silk)) == 100


def test_real_voice_preparation_encodes_tencent_silk(tmp_path):
    pilk = pytest.importorskip("pilk")
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    source = tmp_path / "voice.wav"
    _write_test_wav(source, frames=2400)

    with prepare_weixin_voice(
        str(source),
        limits=WeixinVoiceLimits(max_duration_seconds=1, timeout_seconds=10),
    ) as prepared:
        assert prepared.path.read_bytes().startswith(b"\x02#!SILK_V3")
        assert prepared.playtime_ms == 100
        assert silk_playtime_ms(prepared.path.read_bytes()) == prepared.playtime_ms
        assert pilk.get_duration(str(prepared.path)) == 100
        if os.name != "nt":
            assert prepared.path.stat().st_mode & 0o777 == 0o600
        temporary_root = prepared.path.parent

    assert not temporary_root.exists()


@pytest.mark.asyncio
async def test_upload_voice_item_uses_supplied_playtime(tmp_path, monkeypatch):
    from gateway.weixin_ilink import media

    class UploadResponse:
        status = 200
        headers = {"x-encrypted-param": "signed"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def read(self):
            return b""

    class Session:
        def post(self, *_args, **_kwargs):
            return UploadResponse()

    class Client:
        async def post_json(self, **_kwargs):
            return {"upload_param": "upload"}

    source = tmp_path / "voice.silk"
    source.write_bytes(b"\x02#!SILK_V3" + (4).to_bytes(2, "little", signed=True) + b"data")
    monkeypatch.setattr(media, "validate_media_url", lambda _url: "host")

    item = await upload_media_item(
        Session(),
        Client(),
        to_user_id="peer",
        path=str(source),
        voice_playtime_ms=20,
    )

    assert item["voice_item"]["playtime"] == 20
    with pytest.raises(WeixinMediaError, match="voice_playtime_mismatch"):
        await upload_media_item(
            Session(),
            Client(),
            to_user_id="peer",
            path=str(source),
            voice_playtime_ms=21,
        )
