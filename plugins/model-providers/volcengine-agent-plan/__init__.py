"""Volcengine Ark Agent Plan provider profile."""

from hermes_cli.model_plane.capability import register_code_provider
from providers import register_provider
from providers.base import ProviderProfile


volcengine_agent_plan = ProviderProfile(
    name="volcengine-agent-plan",
    aliases=("volcengine", "ark-agent-plan", "volcengine-ark"),
    api_mode="codex_responses",
    display_name="Volcengine Agent Plan",
    description="Volcengine Ark Agent Plan subscription models",
    signup_url="https://console.volcengine.com/ark/region:cn-beijing/overview",
    env_vars=("VOLCENGINE_AGENT_PLAN_API_KEY",),
    base_url="https://ark.cn-beijing.volces.com/api/plan/v3",
    supports_model_listing=False,
    supports_health_check=False,
    supports_vision=True,
    code_models=("ark-code-latest", "kimi-k2.7-code", "doubao-seed-2.0-code"),
    fallback_models=(
        "ark-code-latest",
        "doubao-seed-2.0-mini",
        "doubao-seed-2.0-lite",
        "doubao-seed-2.1-turbo",
        "doubao-seed-evolving",
        "minimax-m3",
        "glm-5.2",
        "glm-latest",
        "kimi-k2.7-code",
        "kimi-k3",
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "doubao-seed-2.0-code",
        "doubao-seed-2.0-pro",
        "minimax-m2.7",
        "kimi-k2.6",
    ),
    default_aux_model="doubao-seed-2.0-mini",
    image_generation_model="doubao-seedream-5.0-lite",
    image_generation_path="/images/generations",
    embedding_model="doubao-embedding-vision",
    embedding_path="/embeddings/multimodal",
    embedding_dimensions=(1024, 2048),
    tts_model="doubao-seed-tts-2.0",
    tts_url="https://openspeech.bytedance.com/api/v3/plan/tts/unidirectional",
    tts_resource_id="seed-tts-2.0",
    tts_default_voice="zh_female_vv_uranus_bigtts",
    transcription_model="doubao-seed-asr-2.0",
    transcription_url=(
        "wss://openspeech.bytedance.com/api/v3/plan/sauc/bigmodel_nostream"
    ),
    transcription_resource_id="volc.seedasr.sauc.duration",
)

register_provider(volcengine_agent_plan)
register_code_provider(volcengine_agent_plan)
