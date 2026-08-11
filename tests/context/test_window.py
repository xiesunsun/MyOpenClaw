"""apply_window：完整用户轮次与 Tool Loop 不可拆分。"""

from __future__ import annotations

from pickel.context.window import apply_window, group_conversation_turns
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

    turns = group_conversation_turns(messages)

    assert turns == [messages]


def test_apply_window_never_splits_tool_call_from_results():
    messages = [
        _user("u0"),
        _assistant_text("a0"),
        _user("u1"),
        _assistant_tools("c1"),
        _tool_result("c1"),
        _assistant_text("final"),
    ]

    windowed = apply_window(messages, turn_window=1)

    assert len(windowed) == 4
    assert isinstance(windowed[0], UserMessage)
    assert windowed[0].content[0].text == "u1"
    assert isinstance(windowed[1], AssistantMessage)
    assert any(isinstance(b, ToolCallBlock) for b in windowed[1].content)
    assert isinstance(windowed[2], ToolResultMessage)
    assert windowed[2].tool_call_id == "c1"
    assert isinstance(windowed[3], AssistantMessage)
    assert windowed[3].content[0].text == "final"


def test_apply_window_keeps_recent_n_turns():
    messages = [
        _user("u1"),
        _assistant_text("a1"),
        _user("u2"),
        _assistant_text("a2"),
        _user("u3"),
        _assistant_text("a3"),
    ]

    windowed = apply_window(messages, turn_window=2)

    assert [m.content[0].text for m in windowed] == ["u2", "a2", "u3", "a3"]


def test_apply_window_minimum_one_turn():
    messages = [_user("only")]
    assert apply_window(messages, turn_window=0) == messages


def test_window_keeps_user_prompt_across_many_tool_steps():
    messages = [_user("今天星期几了")]
    for index in range(6):
        call_id = f"call-{index}"
        messages.extend([_assistant_tools(call_id), _tool_result(call_id)])
    messages.append(_assistant_text("今天是星期二"))

    windowed = apply_window(messages, turn_window=5)

    assert windowed[0] == messages[0]
    assert windowed == messages
