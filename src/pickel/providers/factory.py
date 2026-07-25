from pickel.providers.anthropic import AnthropicProvider
from pickel.providers.base import Provider
from pickel.providers.gemini import GeminiProvider
from pickel.shared.model_config import ModelConfig


def create_llm_provider(config: ModelConfig) -> Provider:
    if config.provider == "google/gemini":
        return GeminiProvider.from_config(config)
    if config.provider == "anthropic":
        return AnthropicProvider.from_config(config)
    raise ValueError(f"Unsupported LLM provider: {config.provider}")
