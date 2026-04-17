from myopenclaw.providers.base import BaseLLMProvider
from myopenclaw.providers.gemini import GeminiProvider
from myopenclaw.shared.model_config import ModelConfig


def create_llm_provider(config: ModelConfig) -> BaseLLMProvider:
    if config.provider == "google/gemini":
        return GeminiProvider.from_config(config)
    raise ValueError(f"Unsupported LLM provider: {config.provider}")
