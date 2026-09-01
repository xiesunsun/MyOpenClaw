"""Gemini Provider：ModelContext → wire → AssistantMessage。"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

from google import genai
from google.genai import types

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
from pickel.providers.response_json import provider_response_json
from pickel.providers.stream import StreamCompleted
from pickel.shared.model_config import ModelConfig


class GeminiProvider(Provider):
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
        artifact_service: ArtifactService | None = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.api_base = api_base
        self.temperature = 1.0 if temperature is None else temperature
        self.max_output_tokens = max_output_tokens
        self.provider_options = provider_options or {}
        self.artifact_service = artifact_service
        self.client = genai.Client(api_key=api_key) if api_key else genai.Client()

    @classmethod
    def from_config(
        cls,
        config: ModelConfig,
        *,
        artifact_service: ArtifactService | None = None,
    ) -> "GeminiProvider":
        return cls(
            model=config.model,
            api_key=config.api_key,
            api_base=config.api_base,
            temperature=config.temperature,
            max_output_tokens=config.max_output_tokens,
            provider_options=dict(config.provider_options),
            artifact_service=artifact_service,
        )

    def prepare(self, context: ModelContext) -> PreparedModelCall:
        """把唯一 ModelContext 冻结为 Gemini generateContent 请求。"""
        request = self._build_generate_request(context)
        return PreparedModelCall(
            provider="google/gemini",
            api_kind="gemini-generate-content",
            endpoint="generateContent",
            requested_model=self.model,
            body={
                "model": request["model"],
                "contents": self._dump_models(request["contents"]),
                "config": self._dump_model(request["config"]),
            },
        )

    async def stream_prepared(self, prepared: PreparedModelCall) -> Any:
        """通过统一 PreparedModelCall 管道发送非流式 Gemini wire 请求。"""
        if prepared.api_kind != "gemini-generate-content":
            raise ValueError("PreparedModelCall 不是 Gemini generateContent 请求")
        request = thaw_json(prepared.body)
        if not isinstance(request, dict):
            raise ValueError("Gemini PreparedModelCall body 必须是 JSON object")
        response = await self.client.aio.models.generate_content(**request)
        yield StreamCompleted(
            message=self._response_to_assistant_message(response),
            provider_response=provider_response_json(response),
        )

    async def generate(self, context: ModelContext) -> AssistantMessage:
        request = self._build_generate_request(context)
        response = await self.client.aio.models.generate_content(**request)
        return self._response_to_assistant_message(response)

    async def count_context_tokens(self, context: ModelContext) -> int | None:
        request_dict = self._build_count_tokens_request(context)
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

    def _build_generate_request(self, context: ModelContext) -> dict[str, Any]:
        """构造 generate_content 使用的唯一请求参数。"""
        return {
            "model": self.model,
            "contents": self._build_contents(
                context.messages, artifact_service=self.artifact_service
            ),
            "config": self._build_generate_config(context),
        }

    def _build_generate_config(
        self,
        context: ModelContext,
    ) -> types.GenerateContentConfig:
        system_text = context.system.as_text() or None
        config = types.GenerateContentConfig(
            system_instruction=system_text,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True
            ),
        )
        if context.tools:
            config.tools = self._build_tools(context.tools)

        thinking_level = self.provider_options.get("thinking")
        if isinstance(thinking_level, str):
            config.thinking_config = types.ThinkingConfig(thinking_level=thinking_level)
        return config

    def _build_count_tokens_request(self, context: ModelContext) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "generateContentRequest": {
                "model": f"models/{self.model}",
            }
        }
        generate_content_request = payload["generateContentRequest"]

        generate_content_request["contents"] = self._dump_models(
            self._count_tokens_contents(
                context.messages, artifact_service=self.artifact_service
            )
        )

        system_text = context.system.as_text()
        if system_text:
            generate_content_request["systemInstruction"] = self._dump_model(
                types.Content(parts=[types.Part.from_text(text=system_text)])
            )

        if context.tools:
            generate_content_request["tools"] = self._dump_models(
                self._build_tools(context.tools)
            )

        return payload

    @staticmethod
    def _build_tools(tools: list[ToolDefinition]) -> list[types.Tool]:
        return [
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        **GeminiProvider._build_function_declaration(tool)
                    )
                ]
            )
            for tool in tools
        ]

    @staticmethod
    def _build_function_declaration(tool: ToolDefinition) -> dict[str, Any]:
        return {
            "name": tool.name,
            "description": tool.description,
            "parameters_json_schema": thaw_json(tool.input_schema),
        }

    @staticmethod
    def _dump_model(model: Any) -> dict[str, Any]:
        return model.model_dump(mode="json", by_alias=True, exclude_none=True)

    @classmethod
    def _dump_models(cls, models: list[Any]) -> list[dict[str, Any]]:
        return [cls._dump_model(model) for model in models]

    @staticmethod
    def _build_contents(
        messages: list[AgentMessage],
        *,
        artifact_service: ArtifactService | None = None,
    ) -> list[types.Content]:
        """AgentMessage → Gemini Content 列表。

        Assistant 含 tool_call 后，连续 ToolResult 合成一条 user function_response。
        """
        contents: list[types.Content] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            if isinstance(message, UserMessage):
                contents.append(
                    types.Content(
                        role="user",
                        parts=GeminiProvider._message_parts(
                            message.content, artifact_service=artifact_service
                        ),
                    )
                )
                index += 1
                continue

            if isinstance(message, AssistantMessage):
                model_parts = GeminiProvider._assistant_parts(message)
                if model_parts:
                    contents.append(types.Content(role="model", parts=model_parts))
                index += 1
                response_parts: list[types.Part] = []
                while index < len(messages) and isinstance(
                    messages[index], ToolResultMessage
                ):
                    tool_msg = messages[index]
                    assert isinstance(tool_msg, ToolResultMessage)
                    response_parts.extend(
                        GeminiProvider._tool_result_parts(
                            tool_msg, artifact_service=artifact_service
                        )
                    )
                    index += 1
                if response_parts:
                    contents.append(types.Content(role="user", parts=response_parts))
                continue

            if isinstance(message, ToolResultMessage):
                contents.append(
                    types.Content(
                        role="user",
                        parts=GeminiProvider._tool_result_parts(
                            message, artifact_service=artifact_service
                        ),
                    )
                )
                index += 1
                continue

            index += 1
        return contents

    @classmethod
    def _count_tokens_contents(
        cls,
        messages: list[AgentMessage],
        *,
        artifact_service: ArtifactService | None = None,
    ) -> list[types.Content]:
        contents = cls._build_contents(messages, artifact_service=artifact_service)
        if contents:
            return contents
        # Gemini countTokens 需要 contents 字段
        return [
            types.Content(
                role="user",
                parts=[types.Part.from_text(text="")],
            )
        ]

    @staticmethod
    def _assistant_parts(message: AssistantMessage) -> list[types.Part]:
        parts: list[types.Part] = []
        for block in message.content:
            if isinstance(block, ThinkingBlock):
                if block.text:
                    parts.append(types.Part.from_text(text=block.text))
            elif isinstance(block, TextBlock):
                if block.text:
                    parts.append(types.Part.from_text(text=block.text))
            elif isinstance(block, ToolCallBlock):
                thought_signature = None
                if block.thought_signature:
                    try:
                        thought_signature = base64.b64decode(block.thought_signature)
                    except Exception:
                        thought_signature = block.thought_signature.encode("utf-8")
                parts.append(
                    types.Part(
                        function_call=types.FunctionCall(
                            id=block.id,
                            name=block.name,
                            args=thaw_json(block.arguments),
                        ),
                        thought_signature=thought_signature,
                    )
                )
        return parts

    @staticmethod
    def _text_from_blocks(blocks: list[Any]) -> str:
        parts = [
            block.text
            for block in blocks
            if isinstance(block, TextBlock) and block.text
        ]
        return "\n".join(parts)

    @staticmethod
    def _message_parts(
        blocks: list[Any],
        *,
        artifact_service: ArtifactService | None = None,
    ) -> list[types.Part]:
        parts: list[types.Part] = []
        for block in blocks:
            if isinstance(block, TextBlock):
                parts.append(types.Part.from_text(text=block.text))
            elif isinstance(block, ArtifactBlock):
                parts.append(
                    GeminiProvider._artifact_part(
                        block, artifact_service=artifact_service
                    )
                )
        return parts or [types.Part.from_text(text="")]

    @staticmethod
    def _tool_result_parts(
        message: ToolResultMessage,
        *,
        artifact_service: ArtifactService | None = None,
    ) -> list[types.Part]:
        parts = [
            types.Part(
                function_response=types.FunctionResponse(
                    id=message.tool_call_id,
                    name=message.tool_name,
                    response=GeminiProvider._function_response_payload(message),
                )
            )
        ]
        for block in message.content:
            if isinstance(block, ArtifactBlock):
                parts.append(
                    GeminiProvider._artifact_part(
                        block, artifact_service=artifact_service
                    )
                )
        return parts

    @staticmethod
    def _artifact_part(
        block: ArtifactBlock,
        *,
        artifact_service: ArtifactService | None = None,
    ) -> types.Part:
        reference = block.artifact
        if artifact_service is None:
            raise ValueError("Gemini ArtifactBlock 需要 ArtifactService")
        if not reference.media_type.startswith("image/"):
            label = block.alt_text or reference.display_name or reference.artifact_id
            return types.Part.from_text(text=f"[不支持的 Artifact: {label}]")
        return types.Part.from_bytes(
            data=artifact_service.load_artifact_bytes(reference),
            mime_type=reference.media_type,
        )

    @staticmethod
    def _function_response_payload(message: ToolResultMessage) -> dict[str, Any]:
        return {
            "content": [
                {"type": "text", "text": block.text}
                for block in message.content
                if isinstance(block, TextBlock)
            ],
            "is_error": message.is_error,
        }

    def _response_to_assistant_message(
        self, response: types.GenerateContentResponse
    ) -> AssistantMessage:
        content: list[Any] = []
        text = self._extract_text(response)
        if text:
            content.append(TextBlock(text=text))

        for tool_call in self._extract_tool_call_contents(response):
            content.append(tool_call)

        has_tool_calls = any(isinstance(block, ToolCallBlock) for block in content)
        return AssistantMessage(
            content=content,
            metadata=ModelResponseMetadata(
                provider="google/gemini",
                model=self.model,
                provider_model_version=getattr(response, "model_version", None),
                provider_response_id=getattr(response, "response_id", None),
                finish_reason="tool_calls" if has_tool_calls else "stop",
                finish_message=self._extract_provider_finish_message(response),
                usage=self._extract_usage(response),
            ),
        )

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
    def _extract_tool_call_contents(
        response: types.GenerateContentResponse,
    ) -> list[ToolCallBlock]:
        candidates = getattr(response, "candidates", None) or []
        tool_calls: list[ToolCallBlock] = []
        if candidates and candidates[0].content:
            for part in candidates[0].content.parts:
                function_call = getattr(part, "function_call", None)
                if function_call is None:
                    continue
                thought_sig = getattr(part, "thought_signature", None)
                thought_signature_str = None
                if thought_sig is not None:
                    if isinstance(thought_sig, bytes):
                        thought_signature_str = base64.b64encode(thought_sig).decode(
                            "ascii"
                        )
                    else:
                        thought_signature_str = str(thought_sig)
                tool_calls.append(
                    ToolCallBlock(
                        id=function_call.id or function_call.name,
                        name=function_call.name,
                        arguments=dict(function_call.args or {}),
                        thought_signature=thought_signature_str,
                    )
                )
        if tool_calls:
            return tool_calls

        function_calls = getattr(response, "function_calls", None)
        if function_calls:
            return [
                ToolCallBlock(
                    id=function_call.id or function_call.name,
                    name=function_call.name,
                    arguments=dict(function_call.args or {}),
                )
                for function_call in function_calls
            ]
        return []

    @staticmethod
    def _extract_provider_finish_message(
        response: types.GenerateContentResponse,
    ) -> str | None:
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
    def _extract_usage(response: types.GenerateContentResponse) -> ModelUsage | None:
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return None
        return ModelUsage(
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
            cache_read_tokens=getattr(usage, "cached_content_token_count", None),
            reasoning_tokens=getattr(usage, "thoughts_token_count", None),
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
