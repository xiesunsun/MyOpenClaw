from __future__ import annotations

from dataclasses import dataclass

from pickel.conversations.agent_message import (
    AgentMessage,
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextContent, ToolCallContent


@dataclass(frozen=True)
class OpenVikingMessagePayload:
    role: str
    content: str | None
    parts: list[dict]


class OpenVikingMessageMapper:
    """把持久化消息合同直接映射为 OpenViking 消息。"""

    def __init__(self, *, tool_output_max_chars: int = 4000) -> None:
        self._tool_output_max_chars = tool_output_max_chars

    def to_openviking_message(
        self,
        message: AgentMessage,
    ) -> OpenVikingMessagePayload:
        parts: list[dict] = []
        text = self._plain_text(message)
        content = text or None
        if text and not isinstance(message, ToolResultMessage):
            parts.append({"type": "text", "text": text})

        if isinstance(message, AssistantMessage):
            for call in message.content:
                if not isinstance(call, ToolCallContent):
                    continue
                parts.append(
                    {
                        "type": "tool",
                        "tool_id": call.id,
                        "tool_name": call.name,
                        "tool_input": call.arguments,
                        "tool_output": "",
                        "tool_status": "completed",
                    }
                )
        elif isinstance(message, ToolResultMessage):
            parts.append(
                {
                    "type": "tool",
                    "tool_id": message.tool_call_id,
                    "tool_name": message.tool_name,
                    "tool_input": {},
                    "tool_output": self._truncate_tool_output(text),
                    "tool_status": "error" if message.is_error else "completed",
                }
            )
            content = None

        if not parts:
            parts.append({"type": "text", "text": ""})
        return OpenVikingMessagePayload(
            role="user" if isinstance(message, UserMessage) else "assistant",
            content=content,
            parts=parts,
        )

    @staticmethod
    def _plain_text(message: AgentMessage) -> str:
        return "".join(
            block.text for block in message.content if isinstance(block, TextContent)
        )

    def _truncate_tool_output(self, content: str) -> str:
        if self._tool_output_max_chars < 0:
            return content
        return content[: self._tool_output_max_chars]
