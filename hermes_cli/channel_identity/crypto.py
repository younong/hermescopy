"""Versioned lookup hashing and authenticated encryption for channel state."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from typing import Mapping

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_LOOKUP_KEYS_ENV = "HERMES_ILINK_LOOKUP_KEYS_JSON"
_ENCRYPTION_KEYS_ENV = "HERMES_ILINK_ENCRYPTION_KEYS_JSON"


@dataclass(frozen=True)
class Keyring:
    keys: Mapping[int, bytes]
    active_version: int

    @classmethod
    def from_env(cls, env_name: str, *, active_version: int) -> "Keyring":
        raw = os.environ.get(env_name, "").strip()
        if not raw:
            raise RuntimeError(f"{env_name} is required when the iLink connector is enabled")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{env_name} must contain a JSON object") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"{env_name} must contain a JSON object")
        keys: dict[int, bytes] = {}
        for raw_version, raw_key in payload.items():
            try:
                version = int(raw_version)
                key = base64.b64decode(str(raw_key), validate=True)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(f"{env_name} contains an invalid key entry") from exc
            if version < 1 or len(key) != 32:
                raise RuntimeError(f"{env_name} keys must be versioned 32-byte values")
            keys[version] = key
        if active_version not in keys:
            raise RuntimeError(f"{env_name} does not contain active version {active_version}")
        return cls(keys=keys, active_version=active_version)

    def key(self, version: int) -> bytes:
        try:
            return self.keys[int(version)]
        except (KeyError, ValueError) as exc:
            raise RuntimeError(f"required key version {version} is unavailable") from exc


class ChannelCrypto:
    """Keeps lookup and encryption keys separated by construction."""

    def __init__(self, *, lookup: Keyring, encryption: Keyring) -> None:
        self.lookup = lookup
        self.encryption = encryption

    @classmethod
    def from_env(
        cls,
        *,
        lookup_version: int,
        encryption_version: int,
    ) -> "ChannelCrypto":
        return cls(
            lookup=Keyring.from_env(_LOOKUP_KEYS_ENV, active_version=lookup_version),
            encryption=Keyring.from_env(_ENCRYPTION_KEYS_ENV, active_version=encryption_version),
        )

    def lookup_hash(self, domain: str, value: str, *, version: int | None = None) -> str:
        key_version = version or self.lookup.active_version
        material = f"hermes-ilink\x1f{domain}\x1f{value}".encode("utf-8")
        digest = hmac.new(self.lookup.key(key_version), material, hashlib.sha256).hexdigest()
        return f"h{key_version}_{digest}"

    def encrypt_text(
        self,
        value: str,
        *,
        table: str,
        record_id: str,
        field: str,
        version: int | None = None,
    ) -> tuple[bytes, int]:
        key_version = version or self.encryption.active_version
        nonce = os.urandom(12)
        aad = self._aad(table, record_id, field, key_version)
        ciphertext = AESGCM(self.encryption.key(key_version)).encrypt(
            nonce,
            value.encode("utf-8"),
            aad,
        )
        return nonce + ciphertext, key_version

    def decrypt_text(
        self,
        value: bytes,
        *,
        table: str,
        record_id: str,
        field: str,
        version: int,
    ) -> str:
        if len(value) < 29:
            raise RuntimeError("encrypted channel value is malformed")
        nonce, ciphertext = value[:12], value[12:]
        aad = self._aad(table, record_id, field, version)
        try:
            plaintext = AESGCM(self.encryption.key(version)).decrypt(nonce, ciphertext, aad)
        except Exception as exc:
            raise RuntimeError("encrypted channel value failed authentication") from exc
        return plaintext.decode("utf-8")

    @staticmethod
    def _aad(table: str, record_id: str, field: str, version: int) -> bytes:
        return f"channel_identities.sqlite3\x1f{table}\x1f{record_id}\x1f{field}\x1f{version}".encode(
            "utf-8"
        )
