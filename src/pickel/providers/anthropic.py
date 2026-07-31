"""Anthropic Provider：ModelContext → wire → AssistantMessage。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, AsyncIterator

from anthropic import AsyncAnthropic

from pickel.context.model_context import ModelContext, ToolDefinition
from pickel.conversations.agent_message import (
    AgentMessage,
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import (
    TextContent,
    ThinkingContent,
    ToolCallContent,
)
from pickel.providers.base import Provider
from pickel.providers.stream import (
    StreamCompleted,
    StreamDelta,
    TextDelta,
    ThinkingDelta,
    ToolCallArgsDelta,
    accumulate,
)
from pickel.shared.model_config import ModelConfig


class AnthropicProvider(Provider):
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
        self.cache_control = self._resolve_cache_control()
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

    async def generate(self, context: ModelContext) -> AssistantMessage:
        # 由 stream() 实现：同一个 provider 不得有两份解析逻辑
        return await accumulate(self.stream(context))

    async def stream(self, context: ModelContext) -> AsyncIterator[StreamDelta]:
        # input_json_delta 事件不带 tool_call id，须由 content_block_start 记下
        tool_call_ids: dict[int, str] = {}

        async with self.client.messages.stream(
            **self._build_create_params(context)
        ) as stream:
            async for event in stream:
                event_type = getattr(event, "type", None)

                if event_type == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if getattr(block, "type", None) == "tool_use":
                        index = getattr(event, "index", None)
                        block_id = getattr(block, "id", None)
                        if index is not None and block_id is not None:
                            tool_call_ids[index] = str(block_id)
                    continue

                if event_type != "content_block_delta":
                    continue

                delta = getattr(event, "delta", None)
                delta_type = getattr(delta, "type", None)

                if delta_type == "text_delta":
                    text = getattr(delta, "text", None)
                    if text:
                        yield TextDelta(text=str(text))
                elif delta_type == "thinking_delta":
                    thinking = getattr(delta, "thinking", None)
                    if thinking:
                        yield ThinkingDelta(text=str(thinking))
                elif delta_type == "input_json_delta":
                    partial = getattr(delta, "partial_json", None)
                    if partial:
                        yield ToolCallArgsDelta(
                            tool_call_id=tool_call_ids.get(
                                getattr(event, "index", -1), ""
                            ),
                            partial_json=str(partial),
                        )
                # signature_delta 不单独发：signature 由 SDK 累积进
                # get_final_message()，自行拼装反而会漏

            final = await stream.get_final_message()

        yield StreamCompleted(message=self._response_to_assistant_message(final))

    async def count_context_tokens(self, context: ModelContext) -> int | None:
        try:
            response = await self.client.messages.count_tokens(
                **self._build_count_tokens_params(context)
            )
        except Exception:
            return None
        input_tokens = getattr(response, "input_tokens", None)
        return int(input_tokens) if input_tokens is not None else None

    def request_snapshot(self, context: ModelContext) -> dict[str, Any]:
        """保留 wire 参数，并声明 Anthropic 实际用于缓存匹配的语义顺序。"""
        return {
            "provider": "anthropic",
            "model": self.model,
            "cache_order": ["tools", "system", "messages"],
            "request": self._build_create_params(context),
        }

    def _build_create_params(self, context: ModelContext) -> dict[str, Any]:
        params = self._build_request_params(context)
        params["max_tokens"] = self.max_output_tokens
        if self._should_send_temperature():
            params["temperature"] = self.temperature
        return params

    def _build_count_tokens_params(self, context: ModelContext) -> dict[str, Any]:
        return self._build_request_params(context)

    def _build_request_params(self, context: ModelContext) -> dict[str, Any]:
        cache_control = getattr(self, "cache_control", None)
        params: dict[str, Any] = {
            "model": self.model,
            "messages": self._build_messages(context.messages),
        }
        system_text = context.system.as_text()
        if system_text:
            params["system"] = (
                [
                    {
                        "type": "text",
                        "text": system_text,
                        # system 断点同时覆盖位于它之前的全部工具定义。
                        "cache_control": dict(cache_control),
                    }
                ]
                if cache_control is not None
                else system_text
            )
        if context.tools:
            params["tools"] = self._build_tools(context.tools)
        if cache_control is not None:
            # 顶层自动断点跟随多轮 messages 向前移动。
            params["cache_control"] = dict(cache_control)
        thinking, output_config = self._build_thinking_config()
        if thinking is not None:
            params["thinking"] = thinking
        if output_config is not None:
            params["output_config"] = output_config
        return params

    @staticmethod
    def _build_messages(messages: list[AgentMessage]) -> list[dict[str, Any]]:
        """将 AgentMessage 列表编码为 Anthropic messages。

        连续 ToolResultMessage 合成一条 user（多个 tool_result blocks）。
        """
        payload: list[dict[str, Any]] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            if isinstance(message, UserMessage):
                payload.append(
                    {
                        "role": "user",
                        "content": AnthropicProvider._user_content_blocks(message),
                    }
                )
                index += 1
                continue

            if isinstance(message, AssistantMessage):
                assistant_blocks = AnthropicProvider._assistant_content_blocks(message)
                if assistant_blocks:
                    payload.append({"role": "assistant", "content": assistant_blocks})
                index += 1
                tool_result_blocks: list[dict[str, Any]] = []
                while index < len(messages) and isinstance(
                    messages[index], ToolResultMessage
                ):
                    tool_result_blocks.append(
                        AnthropicProvider._tool_result_block(
                            messages[index]  # type: ignore[arg-type]
                        )
                    )
                    index += 1
                if tool_result_blocks:
                    payload.append({"role": "user", "content": tool_result_blocks})
                continue

            if isinstance(message, ToolResultMessage):
                # 孤立 tool result：仍编码为 user tool_result
                payload.append(
                    {
                        "role": "user",
                        "content": [AnthropicProvider._tool_result_block(message)],
                    }
                )
                index += 1
                continue

            index += 1
        return payload

    @staticmethod
    def _user_content_blocks(message: UserMessage) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, TextContent):
                blocks.append({"type": "text", "text": block.text})
        if not blocks:
            blocks.append({"type": "text", "text": ""})
        return blocks

    @staticmethod
    def _assistant_content_blocks(message: AssistantMessage) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, ThinkingContent):
                thinking_block: dict[str, Any] = {
                    "type": "thinking",
                    "thinking": block.text,
                }
                if block.signature is not None:
                    thinking_block["signature"] = block.signature
                blocks.append(thinking_block)
            elif isinstance(block, TextContent):
                if block.text:
                    blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolCallContent):
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.arguments,
                    }
                )
        return blocks

    @staticmethod
    def _tool_result_block(message: ToolResultMessage) -> dict[str, Any]:
        text_parts = [
            block.text for block in message.content if isinstance(block, TextContent)
        ]
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": message.tool_call_id,
            "content": "\n".join(text_parts),
        }
        if message.is_error:
            block["is_error"] = True
        return block

    @staticmethod
    def _build_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in tools
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

    def _resolve_cache_control(self) -> dict[str, str] | None:
        value = self.provider_options.get("cache_control")
        if value is None or value is False:
            return None
        if not isinstance(value, Mapping):
            raise ValueError("Anthropic provider_options.cache_control 必须是对象")
        unknown = set(value) - {"type", "ttl"}
        if unknown:
            names = ", ".join(sorted(str(item) for item in unknown))
            raise ValueError(f"Anthropic cache_control 包含未知字段: {names}")
        cache_type = value.get("type")
        if cache_type != "ephemeral":
            raise ValueError("Anthropic cache_control.type 目前只支持 'ephemeral'")
        ttl = value.get("ttl")
        if ttl is not None and ttl not in {"5m", "1h"}:
            raise ValueError("Anthropic cache_control.ttl 只支持 '5m' 或 '1h'")
        result = {"type": "ephemeral"}
        if ttl is not None:
            result["ttl"] = str(ttl)
        return result

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

    def _response_to_assistant_message(self, response: Any) -> AssistantMessage:
        content: list[Any] = []
        for block in getattr(response, "content", []) or []:
            block_type = self._block_type(block)
            if block_type == "thinking":
                thinking_text = self._block_field(block, "thinking")
                content.append(
                    ThinkingContent(
                        text=str(thinking_text or ""),
                        signature=(
                            str(self._block_field(block, "signature"))
                            if self._block_field(block, "signature") is not None
                            else None
                        ),
                    )
                )
            elif block_type == "text":
                text = self._block_field(block, "text")
                if text:
                    content.append(TextContent(text=str(text)))
            elif block_type == "tool_use":
                content.append(
                    ToolCallContent(
                        id=str(
                            self._block_field(block, "id")
                            or self._block_field(block, "name")
                        ),
                        name=str(self._block_field(block, "name")),
                        arguments=self._dict_value(self._block_field(block, "input")),
                    )
                )

        has_tool_calls = any(isinstance(block, ToolCallContent) for block in content)
        finish_reason = "tool_calls" if has_tool_calls else "stop"
        return AssistantMessage(
            content=content,
            metadata=ModelResponseMetadata(
                provider="anthropic",
                model=self.model,
                provider_model_version=getattr(response, "model", None),
                provider_response_id=getattr(response, "id", None),
                finish_reason=finish_reason,
                finish_message=None,
                usage=self._extract_usage(response),
            ),
        )

    @staticmethod
    def _extract_usage(response: Any) -> ModelUsage | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None

        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
        cache_write_tokens = getattr(usage, "cache_creation_input_tokens", None)
        cache_read_tokens = getattr(usage, "cache_read_input_tokens", None)

        total_tokens = None
        if input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens

        return ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
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
    def _dict_value(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, Mapping):
            return dict(value)
        return dict(value)
