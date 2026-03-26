from typing import Any

from google import genai
from google.genai import types

from myopenclaw.conversation.message import MessageRole
from myopenclaw.llm.config import ModelConfig
from myopenclaw.llm.provider import BaseLLMProvider, ChatRequest, ChatResult


class GeminiProvider(BaseLLMProvider):
    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        api_base: str | None = None,
        temperature: float = 1.0,
        max_output_tokens: int = 65536,
        provider_options: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.provider_options = provider_options or {}
        self.client = genai.Client(api_key=api_key) if api_key else genai.Client()

    @classmethod
    def from_config(cls, config: ModelConfig) -> "GeminiProvider":
        return cls(
            model=config.model,
            api_key=config.api_key,
            api_base=config.api_base,
            temperature=config.temperature,
            max_output_tokens=config.max_output_tokens,
            provider_options=dict(config.provider_options),
        )

    async def chat(self, request: ChatRequest) -> ChatResult:
        contents: list[types.Content] = []

        for message in request.messages:
            role = "user" if message.role == MessageRole.USER else "model"
            contents.append(
                types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=message.text)],
                )
            )

        config = types.GenerateContentConfig(
            system_instruction=request.system_instruction,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
        )

        thinking_level = self.provider_options.get("thinking_level")
        if isinstance(thinking_level, str):
            config.thinking_config = types.ThinkingConfig(thinking_level=thinking_level)

        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=contents,
            config=config,
        )
        return ChatResult(text=self._extract_text(response), raw=response)

    @staticmethod
    def _extract_text(response: types.GenerateContentResponse) -> str:
        try:
            return response.text or ""
        except (AttributeError, IndexError, TypeError):
            pass

        if response.candidates and response.candidates[0].content:
            for part in response.candidates[0].content.parts:
                if part.text:
                    return part.text
        return ""
