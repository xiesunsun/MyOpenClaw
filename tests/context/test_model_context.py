from pickel.context.model_context import (
    ModelContext,
    SystemContent,
    SystemSection,
    ToolDefinition,
)
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextContent


def test_model_context_holds_system_messages_tools():
    ctx = ModelContext(
        system=SystemContent(sections=[SystemSection(name="behavior", text="you are pickle")]),
        messages=[UserMessage(content=[TextContent(text="hi")])],
        tools=[ToolDefinition(name="read_file", description="read", input_schema={"type": "object"})],
    )
    assert ctx.system.sections[0].name == "behavior"
    assert len(ctx.messages) == 1
    assert ctx.tools[0].name == "read_file"


def test_system_content_from_text_and_as_text():
    empty = SystemContent.from_text("")
    assert empty.sections == []
    assert empty.as_text() == ""

    content = SystemContent.from_text("you are pickle")
    assert len(content.sections) == 1
    assert content.sections[0].name == "system"
    assert content.sections[0].text == "you are pickle"
    assert content.as_text() == "you are pickle"

    multi = SystemContent(
        sections=[
            SystemSection(name="a", text="first"),
            SystemSection(name="b", text="second"),
            SystemSection(name="c", text=""),
        ]
    )
    assert multi.as_text() == "first\n\nsecond"
