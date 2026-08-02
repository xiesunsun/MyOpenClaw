from pickel.conversations.content_blocks import ImageContent, ToolCallContent
from pickel.observe.records import ErrorInfo
from pickel.runs.tool_result import build_tool_result_message
from pickel.tools.base import ToolExecutionResult


def test_build_tool_result_message_projects_only_model_contract():
    call = ToolCallContent(id="c1", name="image", arguments={})
    result = ToolExecutionResult(
        content="ignored fallback",
        metadata={"private": "runtime-only"},
        error=ErrorInfo(kind="tool", type="Failure", message="failed"),
        content_blocks=[ImageContent(media_type="image/png", data_base64="AA==")],
        structured_content={"width": 1},
    )

    message = build_tool_result_message(call, result)

    assert message.tool_call_id == "c1"
    assert message.is_error is True
    assert message.structured_content == {"width": 1}
    assert message.content == result.content_blocks
    assert not hasattr(message, "metadata")
    assert not hasattr(message, "error")
