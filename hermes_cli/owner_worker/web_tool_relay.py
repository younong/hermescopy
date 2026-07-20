"""Compatibility aliases for the generalized owner tool relay."""
from hermes_cli.owner_worker.owner_tool_relay import (
    OWNER_RELAY_TOOL_NAMES,
    OwnerToolRelayBroker,
    OwnerToolRelayError,
    _dispatch_owner_tool,
    _recv_frame,
    _send_frame,
    dispatch_owner_tool_over_relay,
)

WEB_RELAY_TOOL_NAMES = frozenset({"web_search", "web_extract"})
WebToolRelayBroker = OwnerToolRelayBroker
WebToolRelayError = OwnerToolRelayError
dispatch_web_tool_over_relay = dispatch_owner_tool_over_relay


def _dispatch_web_tool(tool_name, arguments):
    """Preserve the former owner-side web dispatch test seam."""
    # Web dispatch does not consume invocation identity or a skill materializer.
    return _dispatch_owner_tool(tool_name, arguments, None, None)  # type: ignore[arg-type]


__all__ = [
    "OWNER_RELAY_TOOL_NAMES",
    "WEB_RELAY_TOOL_NAMES",
    "OwnerToolRelayBroker",
    "OwnerToolRelayError",
    "WebToolRelayBroker",
    "WebToolRelayError",
    "dispatch_owner_tool_over_relay",
    "dispatch_web_tool_over_relay",
]
