from myopenclaw.llm.config import ModelConfig
from myopenclaw.llm.provider import BaseLLMProvider
from myopenclaw.llm.providers.gemini import GeminiProvider


def create_llm_provider(config: ModelConfig) -> BaseLLMProvider:
    if config.provider == "google/gemini":
        return GeminiProvider.from_config(config)
    raise ValueError(f"Unsupported LLM provider: {config.provider}")
