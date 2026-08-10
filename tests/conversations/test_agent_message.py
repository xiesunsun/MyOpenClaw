from pickel.artifacts.artifact import ArtifactReference
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    ToolResultMessage,
    UserMessage,
    agent_message_from_dict,
    agent_message_to_dict,
)
from pickel.conversations.content_blocks import (
    ArtifactBlock,
    ImageContent,
    TextContent,
    ThinkingContent,
    ToolCallContent,
)


def test_user_message_round_trip():
    msg = UserMessage(content=[TextContent(text="hello")])
    restored = agent_message_from_dict(agent_message_to_dict(msg))
    assert restored == msg


def test_user_message_with_image_round_trip():
    msg = UserMessage(
        content=[
            TextContent(text="see image"),
            ImageContent(
                media_type="image/png",
                data_base64="aGVsbG8=",
                url=None,
            ),
        ]
    )
    payload = agent_message_to_dict(msg)
    assert payload["payload_version"] == 3
    assert payload["content"][1]["type"] == "image"
    restored = agent_message_from_dict(payload)
    assert restored == msg


def test_user_message_with_artifact_reference_round_trip():
    reference = ArtifactReference(
        artifact_id="artifact_1",
        digest="a" * 64,
        media_type="image/png",
        size_bytes=42,
        display_name="chart.png",
    )
    msg = UserMessage(content=[ArtifactBlock(artifact=reference, alt_text="chart")])

    payload = agent_message_to_dict(msg)

    assert payload["content"][0]["type"] == "artifact"
    assert payload["content"][0]["artifact"]["artifact_id"] == "artifact_1"
    assert agent_message_from_dict(payload) == msg


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
        structured_content={"count": 1},
    )
    restored = agent_message_from_dict(agent_message_to_dict(msg))
    assert restored == msg


def test_v1_tool_result_message_remains_readable():
    restored = agent_message_from_dict(
        {
            "payload_version": 1,
            "role": "tool",
            "tool_call_id": "c1",
            "tool_name": "read_file",
            "content": [{"type": "text", "text": "ok"}],
            "is_error": False,
        }
    )

    assert isinstance(restored, ToolResultMessage)
    assert restored.structured_content is None
