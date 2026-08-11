"""apply_window：不可拆分单元与 tool 原子组。"""

from __future__ import annotations

from pickel.context.window import apply_window, group_message_units
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


def test_group_units_keeps_tool_call_with_results():
    messages = [
        _user("q"),
        _assistant_tools("c1", "c2"),
        _tool_result("c1", "r1"),
        _tool_result("c2", "r2"),
        _assistant_text("done"),
    ]

    units = group_message_units(messages)

    assert len(units) == 3
    assert units[0] == [messages[0]]
    assert units[1] == [messages[1], messages[2], messages[3]]
    assert units[2] == [messages[4]]


def test_apply_window_never_splits_tool_call_from_results():
    messages = [
        _user("u0"),
        _assistant_text("a0"),
        _user("u1"),
        _assistant_tools("c1"),
        _tool_result("c1"),
        _assistant_text("final"),
    ]

    # 6 messages → units: [u0], [a0], [u1], [assistant+tool], [final] = 5 units
    # window=2 keeps last 2 units: tool group + final
    windowed = apply_window(messages, unit_window=2)

    assert len(windowed) == 3
    assert isinstance(windowed[0], AssistantMessage)
    assert any(isinstance(b, ToolCallBlock) for b in windowed[0].content)
    assert isinstance(windowed[1], ToolResultMessage)
    assert windowed[1].tool_call_id == "c1"
    assert isinstance(windowed[2], AssistantMessage)
    assert windowed[2].content[0].text == "final"


def test_apply_window_keeps_recent_n_units():
    messages = [
        _user("u1"),
        _assistant_text("a1"),
        _user("u2"),
        _assistant_text("a2"),
        _user("u3"),
        _assistant_text("a3"),
    ]

    windowed = apply_window(messages, unit_window=3)

    assert [m.content[0].text for m in windowed] == ["a2", "u3", "a3"]


def test_apply_window_minimum_one_unit():
    messages = [_user("only")]
    assert apply_window(messages, unit_window=0) == messages
