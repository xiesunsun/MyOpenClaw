from myopenclaw.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    ToolResultMessage,
    UserMessage,
    agent_message_from_dict,
    agent_message_to_dict,
)
from myopenclaw.conversations.content_blocks import (
    TextContent,
    ThinkingContent,
    ToolCallContent,
)


def test_user_message_round_trip():
    msg = UserMessage(content=[TextContent(text="hello")])
    restored = agent_message_from_dict(agent_message_to_dict(msg))
    assert restored == msg


def test_assistant_with_thinking_and_tool_calls_round_trip():
    msg = AssistantMessage(
        content=[
            ThinkingContent(text="plan", signature="sig"),
            TextContent(text="calling tools"),
            ToolCallContent(id="c1", name="read_file", arguments={"path": "a.py"}),
        ],
        metadata=ModelResponseMetadata(
            provider="anthropic",
            model="claude-test",
            usage=ModelUsage(
                input_tokens=10,
                output_tokens=5,
                cache_read_tokens=3,
                cache_write_tokens=1,
            ),
        ),
    )
    restored = agent_message_from_dict(agent_message_to_dict(msg))
    assert restored == msg


def test_tool_result_message_round_trip():
    msg = ToolResultMessage(
        tool_call_id="c1",
        tool_name="read_file",
        content=[TextContent(text="ok")],
        is_error=False,
    )
    restored = agent_message_from_dict(agent_message_to_dict(msg))
    assert restored == msg
