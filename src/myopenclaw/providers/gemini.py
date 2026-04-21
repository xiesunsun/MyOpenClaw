import asyncio
import json
from typing import Any

from google import genai
from google.genai import types

from myopenclaw.conversations.message import (
    MessageRole,
    SessionMessage,
    ToolCall,
    ToolCallBatch,
    ToolCallResult,
)
from myopenclaw.providers.base import BaseLLMProvider
from myopenclaw.shared.generation import (
    FinishReason,
    GenerateRequest,
    GenerateResult,
    TokenUsage,
)
from myopenclaw.shared.model_config import ModelConfig
from myopenclaw.tools.base import ToolSpec


class GeminiProvider(BaseLLMProvider):
    COUNT_TOKENS_MAX_ATTEMPTS = 3
    COUNT_TOKENS_RETRY_BASE_DELAY_S = 0.2

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        api_base: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int = 65536,
        provider_options: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.temperature = 1.0 if temperature is None else temperature
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
        config = self._build_generate_config(request)
        response = await self.client.aio.models.generate_content(
            model=self.model,
            contents=self._build_contents(request.messages),
            config=config,
        )
        return GenerateResult(
            text=self._extract_text(response),
            tool_calls=self._extract_tool_calls(response),
            finish_reason=self._extract_finish_reason(response),
            provider_finish_reason=self._extract_provider_finish_reason(response),
            provider_finish_message=self._extract_provider_finish_message(response),
            provider_response_id=getattr(response, "response_id", None),
            provider_model_version=getattr(response, "model_version", None),
            usage=self._extract_usage(response),
            raw=response,
        )

    async def count_request_tokens(self, request: GenerateRequest) -> int | None:
        request_dict = self._build_count_tokens_request(request)
        for attempt in range(self.COUNT_TOKENS_MAX_ATTEMPTS):
            try:
                response = await self.client._api_client.async_request(
                    http_method="post",
                    path=f"models/{self.model}:countTokens",
                    request_dict=request_dict,
                )
            except Exception:
                if attempt == self.COUNT_TOKENS_MAX_ATTEMPTS - 1:
                    return None
            else:
                total_tokens = self._extract_count_tokens_total(response)
                if total_tokens is not None:
                    return total_tokens
                if attempt == self.COUNT_TOKENS_MAX_ATTEMPTS - 1:
                    return None

            await asyncio.sleep(self._count_tokens_retry_delay(attempt))
        return None

    @classmethod
    def _count_tokens_retry_delay(cls, attempt: int) -> float:
        return cls.COUNT_TOKENS_RETRY_BASE_DELAY_S * (2**attempt)

    def _build_generate_config(
        self,
        request: GenerateRequest,
    ) -> types.GenerateContentConfig:
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

        thinking_level = self.provider_options.get("thinking")
        if isinstance(thinking_level, str):
            config.thinking_config = types.ThinkingConfig(thinking_level=thinking_level)
        return config

    def _build_count_tokens_request(self, request: GenerateRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "generateContentRequest": {
                "model": f"models/{self.model}",
            }
        }
        generate_content_request = payload["generateContentRequest"]

        generate_content_request["contents"] = self._dump_models(
            self._count_tokens_contents(request.messages)
        )

        if request.system_instruction:
            generate_content_request["systemInstruction"] = self._dump_model(
                types.Content(parts=[types.Part.from_text(text=request.system_instruction)])
            )

        if request.tools:
            generate_content_request["tools"] = self._dump_models(
                self._build_tools(request.tools)
            )

        return payload

    @staticmethod
    def _build_tools(tool_specs: list[ToolSpec]) -> list[types.Tool]:
        return [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(**GeminiProvider._build_function_declaration(tool_spec))
                ]
            )
            for tool_spec in tool_specs
        ]

    @staticmethod
    def _build_function_declaration(tool_spec: ToolSpec) -> dict[str, Any]:
        declaration = {
            "name": tool_spec.name,
            "description": tool_spec.description,
            "parameters_json_schema": tool_spec.input_schema,
        }
        if tool_spec.output_schema is not None:
            declaration["response_json_schema"] = tool_spec.output_schema
        return declaration

    @staticmethod
    def _dump_model(model: Any) -> dict[str, Any]:
        return model.model_dump(mode="json", by_alias=True, exclude_none=True)

    @classmethod
    def _dump_models(cls, models: list[Any]) -> list[dict[str, Any]]:
        return [cls._dump_model(model) for model in models]

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
                if message.tool_call_batch is not None:
                    contents.extend(GeminiProvider._build_batch_contents(message))
                    continue

                contents.append(
                    types.Content(
                        role="model",
                        parts=[types.Part.from_text(text=message.content)],
                    )
                )
        return contents

    @classmethod
    def _count_tokens_contents(
        cls,
        messages: list[SessionMessage],
    ) -> list[types.Content]:
        contents = cls._build_contents(messages)
        if contents:
            return contents
        # Gemini countTokens requires a contents field even when we only want
        # the fixed prompt baseline with no conversation history yet.
        return [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="")],
            )
        ]

    @staticmethod
    def _build_batch_contents(message: SessionMessage) -> list[types.Content]:
        batch = message.tool_call_batch
        if batch is None:
            return []

        model_parts: list[types.Part] = []
        if message.content:
            model_parts.append(types.Part.from_text(text=message.content))
        for tool_call in batch.calls:
            model_parts.append(
                types.Part(
                    function_call=types.FunctionCall(
                        id=tool_call.id,
                        name=tool_call.name,
                        args=tool_call.arguments,
                    ),
                    thought_signature=tool_call.thought_signature,
                )
            )

        response_parts = [
            types.Part(
                function_response=types.FunctionResponse(
                    id=tool_call.id,
                    name=tool_call.name,
                    response=GeminiProvider._build_function_response_payload(tool_result),
                )
            )
            for tool_call, tool_result in GeminiProvider._ordered_batch_pairs(batch)
        ]

        contents = [types.Content(role="model", parts=model_parts)]
        if response_parts:
            contents.append(types.Content(role="user", parts=response_parts))
        return contents

    @staticmethod
    def _ordered_batch_pairs(
        batch: ToolCallBatch,
    ) -> list[tuple[ToolCall, ToolCallResult]]:
        results_by_call_id = {
            tool_result.call_id: tool_result for tool_result in batch.results
        }
        return [
            (tool_call, results_by_call_id[tool_call.id])
            for tool_call in batch.calls
            if tool_call.id in results_by_call_id
        ]

    @staticmethod
    def _build_function_response_payload(tool_result: ToolCallResult) -> dict[str, Any]:
        response = {"error": tool_result.content} if tool_result.is_error else {"output": tool_result.content}
        if tool_result.metadata:
            response["metadata"] = dict(tool_result.metadata)
        return response

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
    def _extract_provider_finish_reason(response: types.GenerateContentResponse) -> str | None:
        candidate = GeminiProvider._primary_candidate(response)
        if candidate is None:
            return None
        finish_reason = getattr(candidate, "finish_reason", None)
        if finish_reason is None:
            return None
        return (
            getattr(finish_reason, "name", None)
            or getattr(finish_reason, "value", None)
            or str(finish_reason)
        )

    @staticmethod
    def _extract_provider_finish_message(response: types.GenerateContentResponse) -> str | None:
        candidate = GeminiProvider._primary_candidate(response)
        if candidate is None:
            return None
        return getattr(candidate, "finish_message", None)

    @staticmethod
    def _primary_candidate(response: types.GenerateContentResponse) -> Any | None:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return None
        return candidates[0]

    @staticmethod
    def _extract_usage(response: types.GenerateContentResponse) -> TokenUsage | None:
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return None
        return TokenUsage(
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
            cached_content_tokens=getattr(usage, "cached_content_token_count", None),
            thoughts_tokens=getattr(usage, "thoughts_token_count", None),
            tool_use_prompt_tokens=getattr(usage, "tool_use_prompt_token_count", None),
            total_tokens=getattr(usage, "total_token_count", None),
        )

    @staticmethod
    def _extract_count_tokens_total(response: Any) -> int | None:
        total_tokens = getattr(response, "total_tokens", None)
        if total_tokens is not None:
            return total_tokens

        body = getattr(response, "body", None)
        if not body:
            return None

        try:
            data = json.loads(body)
        except (TypeError, json.JSONDecodeError):
            return None
        value = data.get("totalTokens")
        return value if isinstance(value, int) else None
