"""Reusable async transport for Tencent's WeChat iLink Bot API."""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import struct
from typing import Any, Mapping
from urllib.parse import quote

from .models import (
    ILinkCredentials,
    ILinkTransportError,
    QRCode,
    QRCodeStatus,
    QRStatus,
    UpdateBatch,
)

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

API_TIMEOUT_MS = 15_000
CONFIG_TIMEOUT_MS = 10_000
QR_TIMEOUT_MS = 35_000

ITEM_TEXT = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2


class WeixinILinkClient:
    """HTTP client shared by the legacy adapter and central connector."""

    def __init__(
        self,
        session: Any,
        *,
        base_url: str = ILINK_BASE_URL,
        token: str = "",
    ) -> None:
        self._session = session
        self.base_url = base_url.rstrip("/")
        self._token = token

    async def create_qr_code(
        self,
        *,
        bot_type: str = "3",
        timeout_ms: int = QR_TIMEOUT_MS,
    ) -> QRCode:
        payload = await self.get_json(
            endpoint=f"{EP_GET_BOT_QR}?bot_type={quote(str(bot_type), safe='')}",
            timeout_ms=timeout_ms,
            operation="create QR code",
        )
        token = _required_string(payload, "qrcode", "create QR code")
        content = str(payload.get("qrcode_img_content") or token)
        return QRCode(token=token, content=content)

    async def get_qr_status(
        self,
        qr_token: str,
        *,
        timeout_ms: int = QR_TIMEOUT_MS,
    ) -> QRStatus:
        if not qr_token:
            raise ValueError("qr_token must not be empty")
        payload = await self.get_json(
            endpoint=f"{EP_GET_QR_STATUS}?qrcode={quote(qr_token, safe='')}",
            timeout_ms=timeout_ms,
            operation="get QR status",
        )
        raw_status = str(payload.get("status") or "wait")
        statuses = {
            "wait": QRCodeStatus.WAITING,
            "scaned": QRCodeStatus.SCANNED,
            "scaned_but_redirect": QRCodeStatus.REDIRECT,
            "expired": QRCodeStatus.EXPIRED,
            "confirmed": QRCodeStatus.CONFIRMED,
        }
        status = statuses.get(raw_status)
        if status is None:
            raise ILinkTransportError("get QR status", "unrecognized status")
        credentials = None
        if status is QRCodeStatus.CONFIRMED:
            credentials = ILinkCredentials(
                bot_id=_required_string(payload, "ilink_bot_id", "get QR status"),
                bot_token=_required_string(payload, "bot_token", "get QR status"),
                base_url=str(payload.get("baseurl") or ILINK_BASE_URL).rstrip("/"),
                user_id=str(payload.get("ilink_user_id") or ""),
            )
        return QRStatus(
            status=status,
            redirect_host=str(payload.get("redirect_host") or ""),
            credentials=credentials,
        )

    async def get_updates(
        self,
        cursor: str,
        *,
        timeout_ms: int,
    ) -> UpdateBatch:
        try:
            payload = await self.post_json(
                endpoint=EP_GET_UPDATES,
                payload={"get_updates_buf": cursor},
                timeout_ms=timeout_ms,
                operation="get updates",
            )
        except asyncio.TimeoutError:
            return UpdateBatch(messages=(), cursor=cursor)
        messages = payload.get("msgs") or []
        if not isinstance(messages, list) or any(not isinstance(item, Mapping) for item in messages):
            raise ILinkTransportError("get updates", "malformed message list")
        next_cursor = payload.get("get_updates_buf", cursor)
        if not isinstance(next_cursor, str):
            raise ILinkTransportError("get updates", "malformed cursor")
        suggested = payload.get("longpolling_timeout_ms")
        if isinstance(suggested, bool) or not isinstance(suggested, int) or suggested <= 0:
            suggested = None
        return UpdateBatch(
            messages=tuple(messages),
            cursor=next_cursor,
            long_poll_timeout_ms=suggested,
        )

    async def send_message(
        self,
        *,
        to: str,
        text: str,
        context_token: str | None,
        client_id: str,
    ) -> dict[str, Any]:
        if not text or not text.strip():
            raise ValueError("text must not be empty")
        if not client_id:
            raise ValueError("client_id must not be empty")
        message: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to,
            "client_id": client_id,
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
        }
        if context_token:
            message["context_token"] = context_token
        return await self.post_json(
            endpoint=EP_SEND_MESSAGE,
            payload={"msg": message},
            timeout_ms=API_TIMEOUT_MS,
            operation="send message",
        )

    async def send_typing(
        self,
        *,
        to_user_id: str,
        typing_ticket: str,
        status: int,
    ) -> None:
        await self.post_json(
            endpoint=EP_SEND_TYPING,
            payload={
                "ilink_user_id": to_user_id,
                "typing_ticket": typing_ticket,
                "status": status,
            },
            timeout_ms=CONFIG_TIMEOUT_MS,
            operation="send typing",
        )

    async def post_json(
        self,
        *,
        endpoint: str,
        payload: Mapping[str, Any],
        timeout_ms: int,
        operation: str | None = None,
    ) -> dict[str, Any]:
        body = _json_dumps({**payload, "base_info": _base_info()})
        url = f"{self.base_url}/{endpoint}"
        label = operation or f"POST {endpoint.split('?', 1)[0]}"

        async def _do() -> dict[str, Any]:
            async with self._session.post(url, data=body, headers=_headers(self._token, body)) as response:
                raw = await response.text()
                if not response.ok:
                    raise ILinkTransportError(label, "request failed", http_status=response.status)
                return _decode_json(raw, label)

        return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)

    async def get_json(
        self,
        *,
        endpoint: str,
        timeout_ms: int,
        operation: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{endpoint}"
        label = operation or f"GET {endpoint.split('?', 1)[0]}"
        headers = {
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
        }

        async def _do() -> dict[str, Any]:
            async with self._session.get(url, headers=headers) as response:
                raw = await response.text()
                if not response.ok:
                    raise ILinkTransportError(label, "request failed", http_status=response.status)
                return _decode_json(raw, label)

        return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)


def _decode_json(raw: str, operation: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ILinkTransportError(operation, "malformed JSON response") from exc
    if not isinstance(payload, dict):
        raise ILinkTransportError(operation, "malformed JSON response")
    return payload


def _required_string(payload: Mapping[str, Any], key: str, operation: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ILinkTransportError(operation, "incomplete response")
    return value


def _json_dumps(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _base_info() -> dict[str, Any]:
    return {"channel_version": CHANNEL_VERSION}


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _headers(token: str, body: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers
