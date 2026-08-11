"""Model kinds and per-kind contracts for the unified model plane.

Every kind in :data:`KINDS` flows through the same catalog → registration →
activation pipeline. The only per-kind differences live here:

- chat activates through the ``model.default`` selection (owned by providers);
- code activates into the dedicated ``code_agent`` selection section;
- media kinds activate into their ``{kind}_gen`` selection section;
- ``use_gateway`` semantics exist only for generation media (image/video);
- voice models carry a sub-capability (``tts`` or ``asr``).

Adding a kind means extending THIS file plus a capability implementation —
never a parallel registry or catalog.
"""

from __future__ import annotations

CHAT = "chat"
CODE = "code"
IMAGE = "image"
VIDEO = "video"
VOICE = "voice"
VECTOR = "vector"

KINDS = (CHAT, CODE, IMAGE, VIDEO, VOICE, VECTOR)

# Capability kinds are owned by capability plugins; Chat remains owned by
# ordinary provider profiles. Code is capability-owned but is not media.
CAPABILITY_KINDS = (CODE, IMAGE, VIDEO, VOICE, VECTOR)
MEDIA_KINDS = (IMAGE, VIDEO, VOICE, VECTOR)

# Every capability kind has an independent active selection. Media-only
# routing still uses MEDIA_KINDS below; Code never enters that path.
ACTIVATABLE_KINDS = CAPABILITY_KINDS

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
    """Return the config section holding the active selection for *kind*."""
    if kind == CODE:
        return "code_agent"
    if kind not in CAPABILITY_KINDS:
        raise ValueError(f"kind must be one of {CAPABILITY_KINDS}, got {kind!r}")
    return f"{kind}_gen"
