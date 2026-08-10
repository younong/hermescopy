"""Unified model plane — the ONLY entry point for model access.

Five model kinds (chat/image/video/voice/vector) share one catalog, one
registration store, one activation mechanism, and two credential paths
(user-supplied key vs deployment-managed relay). Chat models are owned by
**providers**; media kinds (image/video/voice/vector) are owned by
**capability plugins**. This package consumes both through the narrow
:mod:`hermes_cli.model_plane.capability` protocol and never imports plugin
implementations directly.

Extension rules (enforced by review, documented in ``docs/model-plane.md``):

- New model kinds, providers, or media capabilities register HERE — never
  through a parallel registry, broker, or catalog.
- Credentials reach runtime code exactly two ways: a per-user configured
  key, or a deployment-managed route resolved by the Control Plane.
"""

from hermes_cli.model_plane.kinds import (
    ACTIVATABLE_KINDS,
    CHAT,
    GATEWAY_KINDS,
    IMAGE,
    KINDS,
    MEDIA_KINDS,
    RELAY_KINDS,
    VECTOR,
    VIDEO,
    VOICE,
    VOICE_CAPABILITIES,
    selection_section,
)

__all__ = [
    "ACTIVATABLE_KINDS",
    "CHAT",
    "GATEWAY_KINDS",
    "IMAGE",
    "KINDS",
    "MEDIA_KINDS",
    "RELAY_KINDS",
    "VECTOR",
    "VIDEO",
    "VOICE",
    "VOICE_CAPABILITIES",
    "selection_section",
]
