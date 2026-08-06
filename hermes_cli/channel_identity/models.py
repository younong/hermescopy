"""Control Plane channel identity records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
    provider: str
    account_id: str
    provider_account_id: str
    binding_id: str
    conversation_id: str
    credential_version: int


@dataclass(frozen=True)
class ResolvedConnectorAccount:
    provider: str
    account_id: str
    provider_account_id: str
    credentials: dict[str, Any]
    credential_version: int


@dataclass(frozen=True)
class ManagedFeishuAccount:
    account_id: str
    canonical_user_id: str
    owner_key: str
    provider_account_id: str
    credential_version: int
    account_status: str
    lifecycle_status: str
    profile_revision: int | None
    profile_fingerprint: str | None


@dataclass(frozen=True)
class EmployeeProfile:
    account_id: str
    revision: int
    fingerprint: str
    lifecycle_status: str
    profile: dict[str, Any]
