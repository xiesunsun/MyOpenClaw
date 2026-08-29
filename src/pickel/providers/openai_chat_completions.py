"""OpenAI-compatible Chat Completions wire Provider。"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any, AsyncIterator

import httpx

from pickel.artifacts.artifact_service import ArtifactService
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
    ArtifactBlock,
    TextBlock,
    ThinkingBlock,
    ToolCallBlock,
    thaw_json,
)
from pickel.providers.base import Provider
from pickel.providers.prepared import PreparedModelCall
from pickel.providers.stream import (
    StreamCompleted,
    StreamDelta,
    TextDelta,
    ThinkingDelta,
    ToolCallArgsDelta,
)
from pickel.shared.model_config import ModelConfig


class OpenAIChatCompletionsProvider(Provider):
    """把唯一 ModelContext 映射到 OpenAI-compatible Chat Completions。"""

    request_cache_order = ("tools", "messages")
    _IMAGE_MEDIA_TYPES = frozenset(
        {"image/jpeg", "image/png", "image/gif", "image/webp"}
    )

    def __init__(
        self,
        model: str,
        provider_name: str = "openai-compatible",
        api_key: str | None = None,
        api_base: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int = 65536,
        provider_options: dict[str, Any] | None = None,
        artifact_service: ArtifactService | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.model = model
        self.provider_name = provider_name
        self.api_key = api_key
        self.api_base = (api_base or "https://api.openai.com/v1").rstrip("/") + "/"
        self.temperature = temperature
        self.max_output_tokens = max_output_tokens
        self.provider_options = provider_options or {}
        self.artifact_service = artifact_service
        self.client = client or self._build_client()

    @classmethod
    def from_config(
        cls,
        config: ModelConfig,
        *,
        artifact_service: ArtifactService | None = None,
    ) -> "OpenAIChatCompletionsProvider":
        return cls(
            model=config.model,
            provider_name=config.provider,
            api_key=config.api_key,
            api_base=config.api_base,
            temperature=config.temperature,
            max_output_tokens=config.max_output_tokens,
            provider_options=dict(config.provider_options),
            artifact_service=artifact_service,
        )

    def prepare(self, context: ModelContext) -> PreparedModelCall:
        body = self._build_create_request(context)
        body["stream"] = True
        if self.provider_options.get("stream_usage", True):
            body["stream_options"] = {"include_usage": True}
        return PreparedModelCall(
            provider=self.provider_name,
            api_kind="openai-chat-completions",
            endpoint="chat/completions",
            requested_model=self.model,
            body=body,
        )

    async def stream_prepared(
        self, prepared: PreparedModelCall
    ) -> AsyncIterator[StreamDelta]:
        if prepared.api_kind != "openai-chat-completions":
            raise ValueError("PreparedModelCall 不是 Chat Completions 请求")
        request = thaw_json(prepared.body)
        if not isinstance(request, dict) or request.get("stream") is not True:
            raise ValueError("Chat Completions PreparedModelCall 缺少 stream=true")
        text_parts: list[str] = []
        thinking: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        raw_events: list[dict[str, Any]] = []
        response_id = response_model = finish_reason = None
        usage = None
        completed = False
        http_status: int | None = None

        async with self.client.stream(
            "POST", "chat/completions", json=request
        ) as response:
            response.raise_for_status()
            http_status = response.status_code
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw = line.removeprefix("data:").strip()
                if not raw:
                    continue
                if raw == "[DONE]":
                    completed = True
                    break
                event = self._json_object(raw)
                raw_events.append(dict(event))
                response_id = self._string(event.get("id")) or response_id
                response_model = self._string(event.get("model")) or response_model
                usage = self._usage(event.get("usage")) or usage
                choices = event.get("choices")
                if not isinstance(choices, list):
                    continue
                for choice in choices:
                    if not isinstance(choice, Mapping):
                        continue
                    reason = self._string(choice.get("finish_reason"))
                    if reason is not None:
                        finish_reason = reason
                        completed = True
                    delta = choice.get("delta")
                    if not isinstance(delta, Mapping):
                        continue
                    thought = delta.get("reasoning_content", delta.get("reasoning"))
                    if isinstance(thought, str) and thought:
                        thinking.append(thought)
                        yield ThinkingDelta(thought)
                    part = delta.get("content")
                    if isinstance(part, str) and part:
                        text_parts.append(part)
                        yield TextDelta(part)
                    for item in self._call_deltas(delta.get("tool_calls")):
                        call = calls.setdefault(
                            item["index"],
                            {"id": "", "name": "", "arguments": ""},
                        )
                        call["id"] = item["id"] or call["id"]
                        call["name"] += item["name"]
                        call["arguments"] += item["arguments"]
                        if item["arguments"]:
                            yield ToolCallArgsDelta(call["id"], item["arguments"])

        if not completed:
            raise ValueError("Chat Completions 流结束时没有完成标志")
        blocks: list[Any] = []
        if thinking:
            blocks.append(ThinkingBlock("".join(thinking)))
        if text_parts:
            blocks.append(TextBlock("".join(text_parts)))
        for index in sorted(calls):
            call = calls[index]
            blocks.append(
                ToolCallBlock(
                    call["id"] or f"tool_call_{index}",
                    call["name"],
                    self._arguments(call["arguments"]),
                )
            )
        message = AssistantMessage(
            tuple(blocks),
            metadata=ModelResponseMetadata(
                provider=self.provider_name,
                model=self.model,
                provider_model_version=response_model,
                provider_response_id=response_id,
                finish_reason="tool_calls" if calls else finish_reason or "stop",
                finish_message=None,
                usage=usage,
            ),
        )
        yield StreamCompleted(
            message=message,
            provider_response={
                "events": raw_events,
                "id": response_id,
                "model": response_model,
                "finish_reason": finish_reason,
            },
            http_status=http_status,
        )

    def _build_create_request(self, context: ModelContext) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(context),
            "max_tokens": self.max_output_tokens,
        }
        if self.temperature is not None:
            request["temperature"] = self.temperature
        if context.tools:
            request["tools"] = self._tools(context.tools)
        parallel = self.provider_options.get("parallel_tool_calls")
        if parallel is not None:
            request["parallel_tool_calls"] = bool(parallel)
        tool_stream = self.provider_options.get("tool_stream")
        if tool_stream is not None:
            request["tool_stream"] = bool(tool_stream)
        thinking = self.provider_options.get("thinking")
        if isinstance(thinking, Mapping):
            request["thinking"] = dict(thinking)
        elif thinking is not None:
            request["thinking"] = {"type": str(thinking)}
        effort = self.provider_options.get("reasoning_effort")
        if effort is not None:
            request["reasoning_effort"] = str(effort)
        return request

    def _messages(self, context: ModelContext) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        system = context.system.as_text()
        if system:
            result.append({"role": "system", "content": system})
        index = 0
        messages = context.messages
        while index < len(messages):
            message = messages[index]
            if not isinstance(message, ToolResultMessage):
                result.append(self._message(message))
                index += 1
                continue

            artifacts: list[ArtifactBlock] = []
            while index < len(messages) and isinstance(
                messages[index], ToolResultMessage
            ):
                tool_result = messages[index]
                assert isinstance(tool_result, ToolResultMessage)
                result.append(self._message(tool_result))
                artifacts.extend(
                    block
                    for block in tool_result.content
                    if isinstance(block, ArtifactBlock)
                )
                index += 1
            if artifacts:
                result.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "Images returned by the preceding tool calls.",
                            },
                            *(self._artifact(block) for block in artifacts),
                        ],
                    }
                )
        return result

    def _message(self, message: AgentMessage) -> dict[str, Any]:
        if isinstance(message, UserMessage):
            return {"role": "user", "content": self._user_content(message)}
        if isinstance(message, AssistantMessage):
            value: dict[str, Any] = {
                "role": "assistant",
                "content": self._text(message),
            }
            if self.provider_options.get("preserve_thinking"):
                thinking = self._thinking(message)
                if thinking:
                    value["reasoning_content"] = thinking
            calls = [
                {
                    "id": block.id,
                    "type": "function",
                    "function": {
                        "name": block.name,
                        "arguments": json.dumps(
                            thaw_json(block.arguments),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                }
                for block in message.content
                if isinstance(block, ToolCallBlock)
            ]
            if calls:
                value["tool_calls"] = calls
            return value
        if isinstance(message, ToolResultMessage):
            text = self._tool_result(message)
            if any(isinstance(block, ArtifactBlock) for block in message.content):
                marker = "[image result attached after this tool-result group]"
                text = f"{text}\n{marker}" if text else marker
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": text,
            }
        raise TypeError(f"Chat Completions 不支持的消息: {type(message).__name__}")

    def _user_content(self, message: UserMessage) -> str | list[dict[str, Any]]:
        if all(isinstance(block, TextBlock) for block in message.content):
            return "\n".join(block.text for block in message.content)
        result: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                result.append({"type": "text", "text": block.text})
            elif isinstance(block, ArtifactBlock):
                result.append(self._artifact(block))
        return result or ""

    def _artifact(self, block: ArtifactBlock) -> dict[str, Any]:
        reference = block.artifact
        if self.artifact_service is None:
            raise ValueError("Chat Completions ArtifactBlock 需要 ArtifactService")
        if reference.media_type not in self._IMAGE_MEDIA_TYPES:
            label = block.alt_text or reference.display_name or reference.artifact_id
            return {"type": "text", "text": f"[不支持的 Artifact: {label}]"}
        data = base64.b64encode(
            self.artifact_service.load_artifact_bytes(reference)
        ).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {
                "url": f"data:{reference.media_type};base64,{data}",
                "detail": "auto",
            },
        }

    @staticmethod
    def _text(message: AssistantMessage) -> str:
        return "\n".join(
            block.text for block in message.content if isinstance(block, TextBlock)
        )

    @staticmethod
    def _thinking(message: AssistantMessage) -> str:
        return "\n".join(
            block.text for block in message.content if isinstance(block, ThinkingBlock)
        )

    @staticmethod
    def _tool_result(message: ToolResultMessage) -> str:
        parts = [
            block.text for block in message.content if isinstance(block, TextBlock)
        ]
        if message.is_error:
            parts.insert(0, "tool_error: true")
        return "\n".join(parts)

    @staticmethod
    def _tools(tools: tuple[ToolDefinition, ...]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": thaw_json(tool.input_schema),
                },
            }
            for tool in tools
        ]

    @staticmethod
    def _call_deltas(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        result = []
        for item in value:
            if not isinstance(item, Mapping):
                continue
            function = item.get("function")
            function = function if isinstance(function, Mapping) else {}
            result.append(
                {
                    "index": int(item.get("index") or 0),
                    "id": str(item.get("id") or ""),
                    "name": str(function.get("name") or ""),
                    "arguments": str(function.get("arguments") or ""),
                }
            )
        return result

    @staticmethod
    def _arguments(value: str) -> dict[str, Any]:
        try:
            result = json.loads(value or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("Chat Completions 工具参数不是合法 JSON") from exc
        if not isinstance(result, dict):
            raise TypeError("Chat Completions 工具参数必须是 JSON object")
        return result

    @staticmethod
    def _usage(value: Any) -> ModelUsage | None:
        if not isinstance(value, Mapping):
            return None
        prompt = value.get("prompt_tokens_details")
        completion = value.get("completion_tokens_details")
        return ModelUsage(
            input_tokens=OpenAIChatCompletionsProvider._integer(
                value.get("prompt_tokens")
            ),
            output_tokens=OpenAIChatCompletionsProvider._integer(
                value.get("completion_tokens")
            ),
            cache_read_tokens=OpenAIChatCompletionsProvider._integer(
                prompt.get("cached_tokens") if isinstance(prompt, Mapping) else None
            ),
            reasoning_tokens=OpenAIChatCompletionsProvider._integer(
                completion.get("reasoning_tokens")
                if isinstance(completion, Mapping)
                else None
            ),
            total_tokens=OpenAIChatCompletionsProvider._integer(
                value.get("total_tokens")
            ),
        )

    def _build_client(self) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        return httpx.AsyncClient(
            base_url=self.api_base,
            headers=headers,
            timeout=float(self.provider_options.get("timeout_seconds", 120)),
        )

    @staticmethod
    def _json_object(value: str) -> Mapping[str, Any]:
        try:
            result = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("Chat Completions 流事件不是合法 JSON") from exc
        if not isinstance(result, Mapping):
            raise TypeError("Chat Completions 流事件必须是 JSON object")
        return result

    @staticmethod
    def _string(value: Any) -> str | None:
        return str(value) if value is not None else None

    @staticmethod
    def _integer(value: Any) -> int | None:
        return int(value) if isinstance(value, (int, float)) else None
