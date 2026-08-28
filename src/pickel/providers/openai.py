"""OpenAI Responses API Provider。"""

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


class OpenAIResponsesProvider(Provider):
    """把唯一 ModelContext 映射到 OpenAI Responses API。"""

    request_cache_order = ("tools", "instructions", "input")

    _IMAGE_MEDIA_TYPES = frozenset(
        {"image/jpeg", "image/png", "image/gif", "image/webp"}
    )

    def __init__(
        self,
        model: str,
        provider_name: str = "openai",
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
    ) -> "OpenAIResponsesProvider":
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
        return PreparedModelCall(
            provider=self.provider_name,
            api_kind="openai-responses",
            endpoint="responses",
            requested_model=self.model,
            body=body,
        )

    async def count_context_tokens(self, context: ModelContext) -> int:
        """通过 Responses 原生 input_tokens 端点计算同一语义请求。"""
        request = self._build_create_request(context)
        accepted = {
            "input",
            "instructions",
            "model",
            "parallel_tool_calls",
            "reasoning",
            "tools",
        }
        payload = {key: value for key, value in request.items() if key in accepted}
        response = await self.client.post("responses/input_tokens", json=payload)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, Mapping):
            raise TypeError("OpenAI input_tokens 响应必须是 JSON object")
        count = value.get("input_tokens")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("OpenAI input_tokens 响应缺少合法 input_tokens")
        return count

    async def stream_prepared(
        self, prepared: PreparedModelCall
    ) -> AsyncIterator[StreamDelta]:
        if prepared.api_kind != "openai-responses":
            raise ValueError("PreparedModelCall 不是 OpenAI Responses 请求")
        request = thaw_json(prepared.body)
        if not isinstance(request, dict) or request.get("stream") is not True:
            raise ValueError("OpenAI Responses PreparedModelCall 缺少 stream=true")
        tool_call_ids: dict[str, str] = {}
        final_response: Mapping[str, Any] | None = None
        http_status: int | None = None

        async with self.client.stream("POST", "responses", json=request) as response:
            response.raise_for_status()
            http_status = response.status_code
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                raw_event = line.removeprefix("data:").strip()
                if not raw_event or raw_event == "[DONE]":
                    continue
                try:
                    event = json.loads(raw_event)
                except json.JSONDecodeError as exc:
                    raise ValueError("OpenAI Responses 流事件不是合法 JSON") from exc
                if not isinstance(event, Mapping):
                    raise TypeError("OpenAI Responses 流事件必须是 JSON object")
                event_type = event.get("type")
                if event_type == "response.output_item.added":
                    item = event.get("item")
                    if (
                        isinstance(item, Mapping)
                        and item.get("type") == "function_call"
                    ):
                        item_id = item.get("id")
                        call_id = item.get("call_id")
                        if item_id is not None and call_id is not None:
                            tool_call_ids[str(item_id)] = str(call_id)
                    continue
                if event_type == "response.output_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str) and delta:
                        yield TextDelta(delta)
                    continue
                if event_type == "response.reasoning_summary_text.delta":
                    delta = event.get("delta")
                    if isinstance(delta, str) and delta:
                        yield ThinkingDelta(delta)
                    continue
                if event_type == "response.function_call_arguments.delta":
                    delta = event.get("delta")
                    item_id = event.get("item_id")
                    if isinstance(delta, str) and delta:
                        yield ToolCallArgsDelta(
                            tool_call_id=tool_call_ids.get(str(item_id), ""),
                            partial_json=delta,
                        )
                    continue
                if event_type == "response.completed":
                    value = event.get("response")
                    if not isinstance(value, Mapping):
                        raise TypeError(
                            "OpenAI response.completed 缺少 response object"
                        )
                    final_response = value
                    continue
                if event_type in {"response.failed", "response.incomplete"}:
                    value = event.get("response")
                    detail = self._response_error_detail(value)
                    raise RuntimeError(f"OpenAI Responses 未完成: {detail}")
                if event_type == "error":
                    raise RuntimeError(
                        f"OpenAI Responses 流错误: {self._event_error_detail(event)}"
                    )

        if final_response is None:
            raise ValueError("OpenAI Responses 流结束时没有 response.completed")
        yield StreamCompleted(
            message=self._response_to_assistant_message(final_response),
            provider_response=dict(final_response),
            http_status=http_status,
        )

    def _build_create_request(self, context: ModelContext) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.model,
            "input": self._build_input(context.messages),
            "store": False,
            "max_output_tokens": self.max_output_tokens,
        }
        instructions = context.system.as_text()
        if instructions:
            request["instructions"] = instructions
        if self.temperature is not None:
            request["temperature"] = self.temperature
        if context.tools:
            request["tools"] = self._build_tools(context.tools)

        reasoning_effort = self.provider_options.get("reasoning_effort")
        reasoning_summary = self.provider_options.get("reasoning_summary")
        if reasoning_effort is not None or reasoning_summary is not None:
            reasoning = {}
            if reasoning_effort is not None:
                reasoning["effort"] = str(reasoning_effort)
            if reasoning_summary is not None:
                reasoning["summary"] = str(reasoning_summary)
            request["reasoning"] = reasoning
        parallel_tool_calls = self.provider_options.get("parallel_tool_calls")
        if parallel_tool_calls is not None:
            request["parallel_tool_calls"] = bool(parallel_tool_calls)
        return request

    def _build_input(self, messages: tuple[AgentMessage, ...]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for message in messages:
            if isinstance(message, UserMessage):
                items.append(
                    {
                        "role": "user",
                        "content": self._user_content(message),
                    }
                )
                continue
            if isinstance(message, AssistantMessage):
                text = self._assistant_text(message)
                if text:
                    items.append(
                        {
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        }
                    )
                for block in message.content:
                    if isinstance(block, ToolCallBlock):
                        items.append(
                            {
                                "type": "function_call",
                                "call_id": block.id,
                                "name": block.name,
                                "arguments": json.dumps(
                                    thaw_json(block.arguments),
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                            }
                        )
                continue
            if isinstance(message, ToolResultMessage):
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id,
                        "output": self._tool_result_output(message),
                    }
                )
                continue
            raise TypeError(f"OpenAI 不支持的 AgentMessage: {type(message).__name__}")
        return items

    def _user_content(self, message: UserMessage) -> list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        for block in message.content:
            if isinstance(block, TextBlock):
                content.append({"type": "input_text", "text": block.text})
            elif isinstance(block, ArtifactBlock):
                content.append(self._artifact_input(block))
        if not content:
            content.append({"type": "input_text", "text": ""})
        return content

    def _artifact_input(self, block: ArtifactBlock) -> dict[str, Any]:
        reference = block.artifact
        if self.artifact_service is None:
            raise ValueError("OpenAI ArtifactBlock 需要 ArtifactService")
        if reference.media_type not in self._IMAGE_MEDIA_TYPES:
            label = block.alt_text or reference.display_name or reference.artifact_id
            return {
                "type": "input_text",
                "text": f"[OpenAI 不支持的 Artifact: {label} ({reference.media_type})]",
            }
        data = base64.b64encode(
            self.artifact_service.load_artifact_bytes(reference)
        ).decode("ascii")
        return {
            "type": "input_image",
            "image_url": f"data:{reference.media_type};base64,{data}",
            "detail": "auto",
        }

    @staticmethod
    def _assistant_text(message: AssistantMessage) -> str:
        # Responses 的 reasoning item 需要 Provider 专属状态；第一版不把
        # Provider-neutral ThinkingBlock 伪装成普通 assistant 文本。
        return "\n".join(
            block.text
            for block in message.content
            if isinstance(block, TextBlock) and block.text
        )

    def _tool_result_output(
        self, message: ToolResultMessage
    ) -> str | list[dict[str, Any]]:
        content: list[dict[str, Any]] = []
        if message.is_error:
            content.append({"type": "input_text", "text": "tool_error: true"})
        for block in message.content:
            if isinstance(block, TextBlock):
                content.append({"type": "input_text", "text": block.text})
            elif isinstance(block, ArtifactBlock):
                content.append(self._artifact_input(block))
        if not content:
            return ""
        if all(item["type"] == "input_text" for item in content):
            return "\n".join(str(item["text"]) for item in content)
        return content

    @staticmethod
    def _build_tools(tools: tuple[ToolDefinition, ...]) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": thaw_json(tool.input_schema),
            }
            for tool in tools
        ]

    def _response_to_assistant_message(
        self, payload: Mapping[str, Any]
    ) -> AssistantMessage:
        content: list[Any] = []
        output = payload.get("output")
        if not isinstance(output, list):
            raise TypeError("OpenAI Responses 响应缺少 output array")
        for item in output:
            if not isinstance(item, Mapping):
                raise TypeError("OpenAI Responses output item 必须是 JSON object")
            item_type = item.get("type")
            if item_type == "reasoning":
                content.extend(self._reasoning_blocks(item))
            elif item_type == "message":
                content.extend(self._message_blocks(item))
            elif item_type == "function_call":
                content.append(self._tool_call_from_wire(item))

        has_tool_calls = any(isinstance(block, ToolCallBlock) for block in content)
        status = self._string(payload.get("status"))
        finish_reason = "tool_calls" if has_tool_calls else "stop"
        finish_message = None
        if status not in {None, "completed"}:
            finish_reason = status
            finish_message = self._response_error_detail(payload)
        return AssistantMessage(
            content=tuple(content),
            metadata=ModelResponseMetadata(
                provider=self.provider_name,
                model=self.model,
                provider_model_version=self._string(payload.get("model")),
                provider_response_id=self._string(payload.get("id")),
                finish_reason=finish_reason,
                finish_message=finish_message,
                usage=self._usage_from_wire(payload.get("usage")),
            ),
        )

    @staticmethod
    def _message_blocks(item: Mapping[str, Any]) -> list[TextBlock]:
        blocks: list[TextBlock] = []
        value = item.get("content")
        if not isinstance(value, list):
            return blocks
        for part in value:
            if not isinstance(part, Mapping):
                continue
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                blocks.append(TextBlock(str(part["text"])))
        return blocks

    @staticmethod
    def _reasoning_blocks(item: Mapping[str, Any]) -> list[ThinkingBlock]:
        blocks: list[ThinkingBlock] = []
        value = item.get("summary")
        if not isinstance(value, list):
            return blocks
        for part in value:
            if not isinstance(part, Mapping):
                continue
            if part.get("type") == "summary_text" and isinstance(part.get("text"), str):
                blocks.append(ThinkingBlock(str(part["text"])))
        return blocks

    @staticmethod
    def _tool_call_from_wire(value: Mapping[str, Any]) -> ToolCallBlock:
        raw_arguments = value.get("arguments") or "{}"
        try:
            arguments = json.loads(str(raw_arguments))
        except json.JSONDecodeError as exc:
            raise ValueError("OpenAI function_call arguments 不是合法 JSON") from exc
        if not isinstance(arguments, dict):
            raise TypeError("OpenAI function_call arguments 必须是 JSON object")
        return ToolCallBlock(
            id=str(value.get("call_id") or value.get("id") or "tool_call"),
            name=str(value.get("name") or ""),
            arguments=arguments,
        )

    @staticmethod
    def _usage_from_wire(value: Any) -> ModelUsage | None:
        if not isinstance(value, Mapping):
            return None
        input_details = value.get("input_tokens_details")
        output_details = value.get("output_tokens_details")
        return ModelUsage(
            input_tokens=OpenAIResponsesProvider._integer(value.get("input_tokens")),
            output_tokens=OpenAIResponsesProvider._integer(value.get("output_tokens")),
            cache_read_tokens=OpenAIResponsesProvider._integer(
                input_details.get("cached_tokens")
                if isinstance(input_details, Mapping)
                else None
            ),
            reasoning_tokens=OpenAIResponsesProvider._integer(
                output_details.get("reasoning_tokens")
                if isinstance(output_details, Mapping)
                else None
            ),
            total_tokens=OpenAIResponsesProvider._integer(value.get("total_tokens")),
        )

    def _build_client(self) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        timeout = self.provider_options.get("timeout_seconds", 120)
        return httpx.AsyncClient(
            base_url=self.api_base,
            headers=headers,
            timeout=float(timeout),
        )

    @staticmethod
    def _response_error_detail(value: Any) -> str:
        if not isinstance(value, Mapping):
            return "缺少响应详情"
        error = value.get("error")
        if isinstance(error, Mapping) and error.get("message"):
            return str(error["message"])
        incomplete = value.get("incomplete_details")
        if isinstance(incomplete, Mapping) and incomplete.get("reason"):
            return str(incomplete["reason"])
        return str(value.get("status") or "unknown")

    @staticmethod
    def _event_error_detail(value: Mapping[str, Any]) -> str:
        message = value.get("message")
        if message:
            return str(message)
        error = value.get("error")
        if isinstance(error, Mapping) and error.get("message"):
            return str(error["message"])
        return "unknown"

    @staticmethod
    def _string(value: Any) -> str | None:
        return str(value) if value is not None else None

    @staticmethod
    def _integer(value: Any) -> int | None:
        return int(value) if isinstance(value, (int, float)) else None
