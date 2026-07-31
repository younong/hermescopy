"""Structural markers for model-only conversation-loop messages."""

from __future__ import annotations

from typing import Any


TRANSIENT_MODEL_INSTRUCTION = "_transient_model_instruction"

# Older recovery paths have purpose-specific flags that carry the same durable
# transcript contract. Keep recognizing them while new internal instructions
# use the generic marker above.
_TRANSIENT_MESSAGE_FLAGS = (
    TRANSIENT_MODEL_INSTRUCTION,
    "_empty_recovery_synthetic",
    "_empty_terminal_sentinel",
    "_thinking_prefill",
    "_verification_stop_synthetic",
    "_pre_verify_synthetic",
)


def is_transient_message(message: Any) -> bool:
    """Return whether a message is internal, model-only loop scaffolding."""
    return isinstance(message, dict) and any(
        message.get(flag) for flag in _TRANSIENT_MESSAGE_FLAGS
    )


def strip_transient_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project model-only scaffolding out of user-visible conversation history."""
    return [message for message in messages if not is_transient_message(message)]


def transient_model_instruction(content: str) -> dict[str, Any]:
    """Build a structurally marked, request-only user instruction."""
    return {
        "role": "user",
        "content": content,
        TRANSIENT_MODEL_INSTRUCTION: True,
    }


def consume_transient_model_instruction(messages: list[dict[str, Any]]) -> None:
    """Drop the trailing request-only instruction after a successful model call."""
    if (
        messages
        and isinstance(messages[-1], dict)
        and messages[-1].get(TRANSIENT_MODEL_INSTRUCTION)
    ):
        messages.pop()
