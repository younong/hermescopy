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

# Kinds routable through the deployment media relay: the Control Plane holds
# the credential and executes on behalf of the worker. Generation media run
# the route's declared executor; voice/vector run the registered capability
# delegate for the route's provider. Chat has its own inference relay and is
# not part of the media relay.
RELAY_KINDS = (IMAGE, VIDEO, VOICE, VECTOR)

# Voice models are tagged with the sub-capability they serve.
VOICE_CAPABILITIES = ("tts", "asr")

# Names reserved for the native built-in voice backends. The built-in TTS
# handlers live in ``tools/tts_tool.py`` and the built-in STT handlers in
# ``tools/transcription_tools.py``; both alias these sets. Voice capability
# plugins may not register under any of these names — built-ins always win
# at dispatch time, so a colliding registration would be dead weight.
BUILTIN_TTS_PROVIDER_NAMES = frozenset({
    "edge",
    "elevenlabs",
    "openai",
    "minimax",
    "xai",
    "mistral",
    "gemini",
    "neutts",
    "kittentts",
    "piper",
})

BUILTIN_STT_PROVIDER_NAMES = frozenset({
    "local",
    "local_command",
    "groq",
    "openai",
    "mistral",
    "xai",
})

# Default provider per kind when nothing is configured and availability alone
# cannot decide (zero or multiple available providers). Kinds without an entry
# have no implicit default.
FALLBACK_CAPABILITY_PROVIDERS = {"image": "fal"}


def selection_section(kind: str) -> str:
    """Config section holding the active selection for a media kind."""
    if kind not in MEDIA_KINDS:
        raise ValueError(f"kind must be one of {MEDIA_KINDS}, got {kind!r}")
    return f"{kind}_gen"
