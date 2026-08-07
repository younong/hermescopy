"""Credential resolution shared by profile-backed provider capabilities."""

from __future__ import annotations

from providers.base import ProviderProfile


def resolve_profile_api_key(profile: ProviderProfile) -> str:
    """Resolve a profile's API key through the canonical auth path."""
    try:
        from hermes_cli.auth import resolve_api_key_provider_credentials

        credentials = resolve_api_key_provider_credentials(profile.name)
        return str(credentials.get("api_key") or "").strip()
    except Exception:
        return ""
