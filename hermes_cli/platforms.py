"""Canonical messaging platform metadata shared by CLI configuration surfaces."""

from collections import OrderedDict
from typing import NamedTuple


class PlatformInfo(NamedTuple):
    """Metadata for a single messaging platform."""

    label: str
    default_toolset: str


PLATFORMS: OrderedDict[str, PlatformInfo] = OrderedDict([
    (
        "weixin_ilink",
        PlatformInfo(label="Weixin iLink", default_toolset="hermes-weixin-ilink"),
    ),
    (
        "feishu",
        PlatformInfo(label="Feishu", default_toolset="hermes-feishu"),
    ),
    (
        "webhook",
        PlatformInfo(label="Webhook", default_toolset="hermes-webhook"),
    ),
])


def platform_label(key: str, default: str = "") -> str:
    """Return the canonical platform display label, or *default*."""
    info = PLATFORMS.get(key)
    return info.label if info is not None else default


def get_all_platforms() -> "OrderedDict[str, PlatformInfo]":
    """Return the canonical messaging platforms."""
    return OrderedDict(PLATFORMS)
