"""Kilo Code provider profile."""

from hermes_cli.model_plane.capability import register_code_provider
from providers import register_provider
from providers.base import ProviderProfile

kilocode = ProviderProfile(
    name="kilocode",
    aliases=("kilo-code", "kilo", "kilo-gateway"),
    env_vars=("KILOCODE_API_KEY",),
    base_url="https://api.kilo.ai/api/gateway",
    default_aux_model="google/gemini-3-flash-preview",
    chat_enabled=False,
)

register_provider(kilocode)
register_code_provider(kilocode)
