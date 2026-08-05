"""Model-facing listing for canonical messaging targets.

Outbound delivery is owned by the authenticated connector outbox. This module
intentionally provides no direct or standalone platform send path.
"""

import json


SEND_MESSAGE_SCHEMA = {
    "name": "send_message",
    "description": "List known Weixin iLink and Feishu messaging targets.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list"],
                "description": "List known canonical messaging targets.",
            }
        },
        "required": ["action"],
    },
}


def send_message_tool(args, **kw):
    """List targets without bypassing authenticated connector delivery."""
    if args.get("action") != "list":
        return json.dumps(
            {
                "error": (
                    "Direct messaging is unavailable; canonical connector delivery "
                    "must be enqueued through the authenticated control plane."
                )
            }
        )
    try:
        from gateway.channel_directory import format_directory_for_display

        return json.dumps({"targets": format_directory_for_display()})
    except Exception as exc:
        return json.dumps({"error": f"Failed to load channel directory: {exc}"})


__all__ = ["SEND_MESSAGE_SCHEMA", "send_message_tool"]
