"""Encrypted provider-neutral connector account credentials."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from .store import ACCOUNT_CREDENTIAL_AAD_TABLE, ChannelIdentityStore


def encrypt_account_credentials(
    store: ChannelIdentityStore,
    *,
    account_id: str,
    credentials: Mapping[str, Any],
) -> tuple[bytes, int]:
    payload = json.dumps(
        dict(credentials),
        separators=(",", ":"),
        sort_keys=True,
    )
    return store.crypto.encrypt_text(
        payload,
        table=ACCOUNT_CREDENTIAL_AAD_TABLE,
        record_id=account_id,
        field="credentials",
    )


def decrypt_account_credentials(
    store: ChannelIdentityStore,
    *,
    account_id: str,
    ciphertext: bytes,
    key_version: int,
) -> dict[str, Any]:
    payload = store.crypto.decrypt_text(
        ciphertext,
        table=ACCOUNT_CREDENTIAL_AAD_TABLE,
        record_id=account_id,
        field="credentials",
        version=key_version,
    )
    try:
        credentials = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("connector account credentials are invalid") from exc
    if not isinstance(credentials, dict) or any(
        not isinstance(key, str) or not key for key in credentials
    ):
        raise RuntimeError("connector account credentials are invalid")
    return credentials
