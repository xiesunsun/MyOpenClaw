"""Provider 公共导出。

具体 Provider 实现包含 SDK/HTTP 依赖，只在明确请求该实现时导入。
"""

from .config import ModelConfig


def __getattr__(name: str):
    if name == "Provider":
        from .base import Provider

        return Provider
    if name == "AnthropicMessagesProvider":
        from .anthropic import AnthropicMessagesProvider

        return AnthropicMessagesProvider
    if name == "OpenAIResponsesProvider":
        from .openai import OpenAIResponsesProvider

        return OpenAIResponsesProvider
    if name == "OpenAIChatCompletionsProvider":
        from .openai_chat_completions import OpenAIChatCompletionsProvider

        return OpenAIChatCompletionsProvider
    if name == "GeminiProvider":
        from .gemini import GeminiProvider

        return GeminiProvider
    raise AttributeError(name)


__all__ = [
    "AnthropicMessagesProvider",
    "OpenAIResponsesProvider",
    "OpenAIChatCompletionsProvider",
    "GeminiProvider",
    "Provider",
    "ModelConfig",
]
