"""Control Plane channel identity records."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RegisteredChannel:
    canonical_user_id: str
    owner_key: str
    external_identity_id: str
    account_id: str
    binding_id: str
    created: bool


@dataclass(frozen=True)
class ResolvedChannelOwner:
    canonical_user_id: str
    owner_key: str
    external_identity_id: str
    account_id: str
    binding_id: str
    account_base_url: str
    bot_id: str
    bot_token: str
    peer_id: str
    credential_version: int
