"""消息单元分组：完整 User/Model/Tool Loop 不可拆分。"""

from __future__ import annotations

from pickel.context.window import group_message_units
from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock


def _user(text: str) -> UserMessage:
    return UserMessage(content=[TextBlock(text=text)])


def _assistant_text(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text=text)])


def _assistant_tools(*call_ids: str) -> AssistantMessage:
    return AssistantMessage(
        content=[
            ToolCallBlock(id=call_id, name="tool", arguments={}) for call_id in call_ids
        ]
    )


def _tool_result(call_id: str, text: str = "ok") -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=call_id,
        tool_name="tool",
        content=[TextBlock(text=text)],
    )


def test_group_turn_keeps_user_tool_loop_and_final_response_together():
    messages = [
        _user("q"),
        _assistant_tools("c1", "c2"),
        _tool_result("c1", "r1"),
        _tool_result("c2", "r2"),
        _assistant_text("done"),
    ]

    turns = group_message_units(messages)

    assert turns == [messages]


def test_window_keeps_user_prompt_across_many_tool_steps():
    messages = [_user("今天星期几了")]
    for index in range(6):
        call_id = f"call-{index}"
        messages.extend([_assistant_tools(call_id), _tool_result(call_id)])
    messages.append(_assistant_text("今天是星期二"))

    units = group_message_units(messages)

    assert units == [messages]
