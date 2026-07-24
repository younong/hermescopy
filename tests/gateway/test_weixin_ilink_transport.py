"""Tests for the reusable WeChat iLink HTTP transport."""

from __future__ import annotations

import asyncio
import json

import pytest

from gateway.weixin_ilink import ILinkTransportError, QRCodeStatus, WeixinILinkClient


class _Response:
    def __init__(self, *, status: int = 200, body: str = "{}", delay: float = 0) -> None:
        self.status = status
        self.ok = status < 400
        self._body = body
        self._delay = delay

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def text(self) -> str:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._body


class _Session:
    def __init__(self, *responses: _Response) -> None:
        self.responses = list(responses)
        self.post_calls: list[tuple[str, dict]] = []
        self.get_calls: list[tuple[str, dict]] = []

    def _next(self) -> _Response:
        return self.responses.pop(0)

    def post(self, url: str, **kwargs):
        self.post_calls.append((url, kwargs))
        return self._next()

    def get(self, url: str, **kwargs):
        self.get_calls.append((url, kwargs))
        return self._next()


def _json_response(payload: dict) -> _Response:
    return _Response(body=json.dumps(payload))


@pytest.mark.asyncio
async def test_create_qr_code_prefers_complete_image_content():
    session = _Session(
        _json_response(
            {
                "qrcode": "secret-qr-token",
                "qrcode_img_content": "https://weixin.example/complete-qr",
            }
        )
    )

    qr = await WeixinILinkClient(session).create_qr_code(bot_type="3")

    assert qr.token == "secret-qr-token"
    assert qr.content == "https://weixin.example/complete-qr"
    assert session.get_calls[0][0].endswith("get_bot_qrcode?bot_type=3")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_status", "expected"),
    [
        ("wait", QRCodeStatus.WAITING),
        ("scaned", QRCodeStatus.SCANNED),
        ("scaned_but_redirect", QRCodeStatus.REDIRECT),
        ("expired", QRCodeStatus.EXPIRED),
    ],
)
async def test_get_qr_status_parses_provider_states(provider_status, expected):
    client = WeixinILinkClient(_Session(_json_response({"status": provider_status})))

    status = await client.get_qr_status("qr-token")

    assert status.status is expected
    assert status.credentials is None


@pytest.mark.asyncio
async def test_get_qr_status_parses_confirmed_credentials():
    client = WeixinILinkClient(
        _Session(
            _json_response(
                {
                    "status": "confirmed",
                    "ilink_bot_id": "bot-id",
                    "bot_token": "bot-token",
                    "baseurl": "https://regional.example/",
                    "ilink_user_id": "user-id",
                }
            )
        )
    )

    status = await client.get_qr_status("qr-token")

    assert status.status is QRCodeStatus.CONFIRMED
    assert status.credentials is not None
    assert status.credentials.bot_id == "bot-id"
    assert status.credentials.bot_token == "bot-token"
    assert status.credentials.base_url == "https://regional.example"
    assert status.credentials.user_id == "user-id"


@pytest.mark.asyncio
async def test_get_updates_parses_cursor_and_timeout():
    client = WeixinILinkClient(
        _Session(
            _json_response(
                {
                    "msgs": [{"message_id": "msg-1"}],
                    "get_updates_buf": "cursor-2",
                    "longpolling_timeout_ms": 42_000,
                }
            )
        ),
        token="bot-token",
    )

    batch = await client.get_updates("cursor-1", timeout_ms=1000)

    assert batch.messages == ({"message_id": "msg-1"},)
    assert batch.cursor == "cursor-2"
    assert batch.long_poll_timeout_ms == 42_000


@pytest.mark.asyncio
async def test_get_updates_timeout_keeps_existing_cursor():
    client = WeixinILinkClient(_Session(_Response(delay=1)), token="bot-token")

    batch = await client.get_updates("cursor-1", timeout_ms=1)

    assert batch.messages == ()
    assert batch.cursor == "cursor-1"


@pytest.mark.asyncio
async def test_send_reuses_caller_supplied_client_id():
    session = _Session(_json_response({"ret": 0}), _json_response({"ret": 0}))
    client = WeixinILinkClient(session, token="bot-token")

    for _ in range(2):
        await client.send_message(
            to="peer-id",
            text="hello",
            context_token="context-token",
            client_id="stable-client-id",
        )

    request_ids = [json.loads(kwargs["data"])["msg"]["client_id"] for _, kwargs in session.post_calls]
    assert request_ids == ["stable-client-id", "stable-client-id"]


@pytest.mark.asyncio
async def test_http_errors_do_not_expose_response_or_credentials():
    secret_values = ["bot-secret", "qr-secret", "context-secret", "provider-secret"]
    session = _Session(_Response(status=500, body="provider-secret"))
    client = WeixinILinkClient(session, token="bot-secret")

    with pytest.raises(ILinkTransportError) as caught:
        await client.send_message(
            to="peer-id",
            text="hello",
            context_token="context-secret",
            client_id="stable-id",
        )

    rendered = f"{caught.value!r} {caught.value}"
    assert "HTTP 500" in rendered
    assert all(secret not in rendered for secret in secret_values)


@pytest.mark.asyncio
async def test_malformed_json_errors_do_not_expose_raw_body():
    client = WeixinILinkClient(
        _Session(_Response(body="not-json provider-secret")),
        token="bot-secret",
    )

    with pytest.raises(ILinkTransportError, match="malformed JSON response") as caught:
        await client.get_updates("cursor", timeout_ms=1000)

    assert "provider-secret" not in str(caught.value)
    assert "bot-secret" not in str(caught.value)
