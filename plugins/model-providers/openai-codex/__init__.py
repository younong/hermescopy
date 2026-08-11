"""OpenAI Codex (Responses API) provider profile."""

from hermes_cli.model_plane.capability import register_code_provider
from providers import register_provider
from providers.base import ProviderProfile

openai_codex = ProviderProfile(
    name="openai-codex",
    aliases=("codex", "openai_codex"),
    api_mode="codex_responses",
    env_vars=(),  # OAuth external — no API key
    base_url="https://chatgpt.com/backend-api/codex",
    auth_type="oauth_external",
    chat_enabled=True,
    code_models=("gpt-5.3-codex", "gpt-5.3-codex-spark"),
    fallback_models=("gpt-5.3-codex", "gpt-5.3-codex-spark"),
)

register_provider(openai_codex)
register_code_provider(openai_codex)
