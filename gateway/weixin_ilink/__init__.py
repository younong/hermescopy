"""Reusable Tencent WeChat iLink Bot API transport."""

from .client import ILINK_BASE_URL, WeixinILinkClient
from .media import (
    WEIXIN_CDN_BASE_URL,
    WeixinMediaError,
    WeixinMediaLimits,
    download_and_decrypt_voice,
)
from .models import (
    ILinkCredentials,
    ILinkTransportError,
    QRCode,
    QRCodeStatus,
    QRStatus,
    UpdateBatch,
)

__all__ = [
    "ILINK_BASE_URL",
    "ILinkCredentials",
    "ILinkTransportError",
    "QRCode",
    "QRCodeStatus",
    "QRStatus",
    "UpdateBatch",
    "WEIXIN_CDN_BASE_URL",
    "WeixinILinkClient",
    "WeixinMediaError",
    "WeixinMediaLimits",
    "download_and_decrypt_voice",
]
