from __future__ import annotations

from dataclasses import dataclass

from myopenclaw.conversations.message import MessageRole, SessionMessage, ToolCallResult


@dataclass(frozen=True)
class OpenVikingMessagePayload:
    role: str
    content: str | None
    parts: list[dict]


class SessionMessageMapper:
    def __init__(self, *, tool_output_max_chars: int = 4000) -> None:
        self._tool_output_max_chars = tool_output_max_chars

    def to_openviking_message(self, message: SessionMessage) -> OpenVikingMessagePayload:
        parts: list[dict] = []
        content = message.content or None
        if message.content:
            parts.append({"type": "text", "text": message.content})
        if message.tool_call_batch is not None:
            results_by_call_id = {
                result.call_id: result for result in message.tool_call_batch.results
            }
            for call in message.tool_call_batch.calls:
                result = results_by_call_id.get(call.id)
                parts.append(
                    {
                        "type": "tool",
                        "tool_id": call.id,
                        "tool_name": call.name,
                        "tool_input": call.arguments,
                        "tool_output": self._tool_output(result),
                        "tool_status": self._tool_status(result),
                    }
                )
        if not parts:
            parts.append({"type": "text", "text": ""})
        return OpenVikingMessagePayload(
            role=self._role_to_openviking(message.role),
            content=content,
            parts=parts,
        )

    def _tool_output(self, result: ToolCallResult | None) -> str:
        if result is None:
            return ""
        if self._tool_output_max_chars < 0:
            return result.content
        return result.content[: self._tool_output_max_chars]

    @staticmethod
    def _tool_status(result: ToolCallResult | None) -> str:
        if result is None or not result.is_error:
            return "completed"
        return "error"

    @staticmethod
    def _role_to_openviking(role: MessageRole) -> str:
        if role in {MessageRole.USER, MessageRole.ASSISTANT}:
            return role.value
        return str(role.value)
