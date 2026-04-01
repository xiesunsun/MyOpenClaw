from typing import Any

from google import genai
from google.genai import types

from myopenclaw.conversation.message import MessageRole, SessionMessage, ToolCall
from myopenclaw.llm.config import ModelConfig
from myopenclaw.llm.provider import BaseLLMProvider
from myopenclaw.runtime.generation import (
    FinishReason,
    GenerateRequest,
    GenerateResult,
    TokenUsage,
)
from myopenclaw.tools.base import ToolSpec


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

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        config = types.GenerateContentConfig(
            system_instruction=request.system_instruction,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )
        if request.tools:
            config.tools = self._build_tools(request.tools)

        thinking_level = self.provider_options.get("thinking_level")
        if isinstance(thinking_level, str):
            config.thinking_config = types.ThinkingConfig(thinking_level=thinking_level)

        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=self._build_contents(request.messages),
            config=config,
        )
        return GenerateResult(
            text=self._extract_text(response),
            tool_calls=self._extract_tool_calls(response),
            finish_reason=self._extract_finish_reason(response),
            usage=self._extract_usage(response),
            raw=response,
        )

    @staticmethod
    def _build_tools(tool_specs: list[ToolSpec]) -> list[types.Tool]:
        return [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name=tool_spec.name,
                        description=tool_spec.description,
                        parameters_json_schema=tool_spec.input_schema,
                    )
                ]
            )
            for tool_spec in tool_specs
        ]

    @staticmethod
    def _build_contents(messages: list[SessionMessage]) -> list[types.Content]:
        contents: list[types.Content] = []
        for message in messages:
            if message.role == MessageRole.USER:
                contents.append(
                    types.Content(
                        role="user",
                        parts=[types.Part.from_text(text=message.content)],
                    )
                )
                continue

            if message.role == MessageRole.ASSISTANT:
                parts: list[types.Part] = []
                if message.content:
                    parts.append(types.Part.from_text(text=message.content))
                for tool_call in message.tool_calls:
                    parts.append(
                        types.Part(
                            function_call=types.FunctionCall(
                                id=tool_call.id,
                                name=tool_call.name,
                                args=tool_call.arguments,
                            ),
                            thought_signature=tool_call.thought_signature,
                        )
                    )
                contents.append(types.Content(role="model", parts=parts))
                continue

            if message.role == MessageRole.TOOL:
                contents.append(
                    types.Content(
                        role="user",
                        parts=[
                            types.Part(
                                function_response=types.FunctionResponse(
                                    id=message.tool_call_id,
                                    name=message.tool_name,
                                    response={
                                        "content": message.content,
                                        "is_error": message.is_error,
                                        "metadata": dict(message.tool_result_metadata),
                                    },
                                )
                            )
                        ],
                    )
                )
        return contents

    @staticmethod
    def _extract_text(response: types.GenerateContentResponse) -> str:
        if response.candidates and response.candidates[0].content:
            texts: list[str] = []
            for part in response.candidates[0].content.parts:
                if part.text:
                    texts.append(part.text)
            return "\n".join(texts)

        try:
            return response.text or ""
        except (AttributeError, IndexError, TypeError):
            pass
        return ""

    @staticmethod
    def _extract_tool_calls(response: types.GenerateContentResponse) -> list[ToolCall]:
        candidates = getattr(response, "candidates", None) or []
        tool_calls: list[ToolCall] = []
        if candidates and candidates[0].content:
            for part in candidates[0].content.parts:
                function_call = getattr(part, "function_call", None)
                if function_call is None:
                    continue
                tool_calls.append(
                    ToolCall(
                        id=function_call.id or function_call.name,
                        name=function_call.name,
                        arguments=dict(function_call.args or {}),
                        thought_signature=getattr(part, "thought_signature", None),
                    )
                )
        if tool_calls:
            return tool_calls

        function_calls = getattr(response, "function_calls", None)
        if function_calls:
            return [
                ToolCall(
                    id=function_call.id or function_call.name,
                    name=function_call.name,
                    arguments=dict(function_call.args or {}),
                )
                for function_call in function_calls
            ]

        return []

    @classmethod
    def _extract_finish_reason(cls, response: types.GenerateContentResponse) -> FinishReason:
        if cls._extract_tool_calls(response):
            return FinishReason.TOOL_CALLS
        return FinishReason.STOP

    @staticmethod
    def _extract_usage(response: types.GenerateContentResponse) -> TokenUsage | None:
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return None
        return TokenUsage(
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
        )
