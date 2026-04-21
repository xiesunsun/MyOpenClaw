from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from anthropic import AsyncAnthropic

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


class AnthropicProvider(BaseLLMProvider):
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
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.provider_options = provider_options or {}
        self.client = self._build_client()

    @classmethod
    def from_config(cls, config: ModelConfig) -> "AnthropicProvider":
        return cls(
            model=config.model,
            api_key=config.api_key,
            api_base=config.api_base,
            temperature=config.temperature,
            max_output_tokens=config.max_output_tokens,
            provider_options=dict(config.provider_options),
        )

    async def generate(self, request: GenerateRequest) -> GenerateResult:
        response = await self.client.messages.create(
            **self._build_create_params(request)
        )
        tool_calls = self._extract_tool_calls(response)
        return GenerateResult(
            text=self._extract_text(response),
            tool_calls=tool_calls,
            finish_reason=(
                FinishReason.TOOL_CALLS if tool_calls else FinishReason.STOP
            ),
            provider_finish_reason=getattr(response, "stop_reason", None),
            provider_finish_message=None,
            provider_response_id=getattr(response, "id", None),
            provider_model_version=getattr(response, "model", None),
            usage=self._extract_usage(response),
            provider_thinking_blocks=self._extract_thinking_blocks(response),
            raw=response,
        )

    async def count_request_tokens(self, request: GenerateRequest) -> int | None:
        try:
            response = await self.client.messages.count_tokens(
                **self._build_count_tokens_params(request)
            )
        except Exception:
            return None
        input_tokens = getattr(response, "input_tokens", None)
        return int(input_tokens) if input_tokens is not None else None

    def _build_create_params(self, request: GenerateRequest) -> dict[str, Any]:
        params = self._build_request_params(request)
        params["max_tokens"] = self.max_output_tokens
        if self._should_send_temperature():
            params["temperature"] = self.temperature
        return params

    def _build_count_tokens_params(self, request: GenerateRequest) -> dict[str, Any]:
        return self._build_request_params(request)

    def _build_request_params(self, request: GenerateRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "messages": self._build_messages(request.messages),
        }
        if request.system_instruction:
            params["system"] = request.system_instruction
        if request.tools:
            params["tools"] = self._build_tools(request.tools)
        thinking, output_config = self._build_thinking_config()
        if thinking is not None:
            params["thinking"] = thinking
        if output_config is not None:
            params["output_config"] = output_config
        return params

    @staticmethod
    def _build_messages(messages: list[SessionMessage]) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for message in messages:
            if message.role == MessageRole.USER:
                payload.append(
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": message.content}],
                    }
                )
                continue

            if message.role != MessageRole.ASSISTANT:
                continue

            assistant_blocks = AnthropicProvider._build_assistant_message_blocks(
                message
            )
            if assistant_blocks:
                payload.append({"role": "assistant", "content": assistant_blocks})

            tool_result_blocks = AnthropicProvider._build_tool_result_blocks(
                message.tool_call_batch
            )
            if tool_result_blocks:
                payload.append({"role": "user", "content": tool_result_blocks})
        return payload

    @staticmethod
    def _build_tools(tool_specs: list[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "name": tool_spec.name,
                "description": tool_spec.description,
                "input_schema": tool_spec.input_schema,
            }
            for tool_spec in tool_specs
        ]

    @staticmethod
    def _build_assistant_message_blocks(message: SessionMessage) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        if message.provider_thinking_blocks:
            blocks.extend(dict(block) for block in message.provider_thinking_blocks)
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        if message.tool_call_batch is not None:
            for tool_call in message.tool_call_batch.calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tool_call.id,
                        "name": tool_call.name,
                        "input": tool_call.arguments,
                    }
                )
        return blocks

    @classmethod
    def _build_tool_result_blocks(
        cls,
        batch: ToolCallBatch | None,
    ) -> list[dict[str, Any]]:
        if batch is None:
            return []
        return [
            cls._build_tool_result_block(tool_call=tool_call, tool_result=tool_result)
            for tool_call, tool_result in cls._ordered_batch_pairs(batch)
        ]

    @staticmethod
    def _build_tool_result_block(
        *,
        tool_call: ToolCall,
        tool_result: ToolCallResult,
    ) -> dict[str, Any]:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool_call.id,
            "content": tool_result.content,
        }
        if tool_result.is_error:
            block["is_error"] = True
        return block

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

    def _build_thinking_config(
        self,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        effort = self.provider_options.get("thinking")
        if not isinstance(effort, str):
            return None, None
        return (
            {"type": "adaptive", "display": "summarized"},
            {"effort": effort},
        )

    def _build_client(self) -> AsyncAnthropic:
        kwargs: dict[str, Any] = {}
        if self.api_key is not None:
            kwargs["api_key"] = self.api_key
        if self.api_base is not None:
            kwargs["base_url"] = self.api_base

        timeout_seconds = self.provider_options.get("timeout_seconds")
        if timeout_seconds is not None:
            kwargs["timeout"] = timeout_seconds

        max_retries = self.provider_options.get("max_retries")
        if max_retries is not None:
            kwargs["max_retries"] = max_retries

        return AsyncAnthropic(**kwargs)

    def _should_send_temperature(self) -> bool:
        return self.temperature is not None and self.model != "claude-opus-4-7"

    @classmethod
    def _extract_text(cls, response: Any) -> str:
        texts = [
            str(text)
            for block in getattr(response, "content", [])
            if cls._block_type(block) == "text"
            for text in [cls._block_field(block, "text")]
            if text
        ]
        return "\n".join(texts)

    @classmethod
    def _extract_tool_calls(cls, response: Any) -> list[ToolCall]:
        tool_calls: list[ToolCall] = []
        for block in getattr(response, "content", []):
            if cls._block_type(block) != "tool_use":
                continue
            tool_calls.append(
                ToolCall(
                    id=str(cls._block_field(block, "id") or cls._block_field(block, "name")),
                    name=str(cls._block_field(block, "name")),
                    arguments=cls._dict_value(cls._block_field(block, "input")),
                )
            )
        return tool_calls

    @classmethod
    def _extract_thinking_blocks(cls, response: Any) -> list[dict[str, Any]] | None:
        blocks = [
            cls._json_dict(block)
            for block in getattr(response, "content", [])
            if cls._block_type(block) == "thinking"
        ]
        return blocks or None

    @staticmethod
    def _extract_usage(response: Any) -> TokenUsage | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None

        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
        cache_creation_tokens = getattr(usage, "cache_creation_input_tokens", None)
        cache_read_tokens = getattr(usage, "cache_read_input_tokens", None)
        cached_content_tokens = None
        if cache_creation_tokens is not None or cache_read_tokens is not None:
            cached_content_tokens = (cache_creation_tokens or 0) + (
                cache_read_tokens or 0
            )

        total_tokens = None
        if input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens

        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_content_tokens=cached_content_tokens,
            total_tokens=total_tokens,
        )

    @staticmethod
    def _block_type(block: Any) -> str | None:
        value = AnthropicProvider._block_field(block, "type")
        return str(value) if value is not None else None

    @staticmethod
    def _block_field(block: Any, name: str) -> Any:
        if isinstance(block, Mapping):
            return block.get(name)
        return getattr(block, name, None)

    @staticmethod
    def _dict_value(value: Any) -> dict[str, object]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, Mapping):
            return dict(value)
        return dict(value)

    @staticmethod
    def _json_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, Mapping):
            return dict(value)
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return dict(model_dump(mode="json", by_alias=True, exclude_none=True))
        return {
            key: field
            for key, field in vars(value).items()
            if not key.startswith("_") and field is not None
        }
