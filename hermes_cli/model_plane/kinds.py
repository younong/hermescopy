"""Model kinds and per-kind contracts for the unified model plane.

Every kind in :data:`KINDS` flows through the same catalog → registration →
activation pipeline. The only per-kind differences live here:

- chat activates through the ``model.default`` selection (owned by providers);
- media kinds activate into their ``{kind}_gen`` selection section;
- ``use_gateway`` semantics exist only for generation media (image/video);
- voice models carry a sub-capability (``tts`` or ``asr``).

Adding a kind means extending THIS file plus a capability implementation —
never a parallel registry or catalog.
"""

from __future__ import annotations

CHAT = "chat"
IMAGE = "image"
VIDEO = "video"
VOICE = "voice"
VECTOR = "vector"

KINDS = (CHAT, IMAGE, VIDEO, VOICE, VECTOR)

# Media kinds are owned by capability plugins; chat is owned by providers.
MEDIA_KINDS = (IMAGE, VIDEO, VOICE, VECTOR)

# Every media kind can be activated into its selection section. Chat stays
# special: its activation target is the model.default selection.
ACTIVATABLE_KINDS = MEDIA_KINDS

# ``use_gateway`` (route generation through the gateway proxy instead of a
# direct provider call) is meaningful only for generation media.
GATEWAY_KINDS = (IMAGE, VIDEO)

# Voice models are tagged with the sub-capability they serve.
VOICE_CAPABILITIES = ("tts", "asr")


def selection_section(kind: str) -> str:
    """Config section holding the active selection for a media kind."""
    if kind not in MEDIA_KINDS:
        raise ValueError(f"kind must be one of {MEDIA_KINDS}, got {kind!r}")
    return f"{kind}_gen"
