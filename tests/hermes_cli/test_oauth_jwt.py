from __future__ import annotations

import base64
import json

import pytest

from hermes_cli.oauth_jwt import decode_unverified_oauth_jwt_claims


def _b64url(value: object) -> str:
    raw = json.dumps(value).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _token(payload: object, signature: str = "invalidsignature") -> str:
    return f"{_b64url({'alg': 'RS256', 'typ': 'JWT'})}.{_b64url(payload)}.{signature}"


def test_decodes_unverified_metadata_claims() -> None:
    claims = {"sub": "user-1", "exp": 1, "scope": "inference:invoke"}

    assert decode_unverified_oauth_jwt_claims(_token(claims)) == claims


def test_does_not_reject_expired_or_invalidly_signed_metadata() -> None:
    token = _token({"exp": 1}, signature="definitelynotavalidsignature")

    assert decode_unverified_oauth_jwt_claims(token) == {"exp": 1}


@pytest.mark.parametrize(
    "token",
    [
        None,
        123,
        "",
        "not-a-jwt",
        "only.two",
        "too.many.jwt.parts",
        "header.%%%not-base64%%%.signature",
        f"{_b64url({})}.{_b64url('not a mapping')}.signature",
        f"{_b64url({})}.{_b64url(['not', 'a', 'mapping'])}.signature",
    ],
)
def test_invalid_tokens_return_empty_mapping(token: object) -> None:
    assert decode_unverified_oauth_jwt_claims(token) == {}
