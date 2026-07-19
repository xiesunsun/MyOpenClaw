"""ContextAssembler：唯一 ModelContext 组装路径。"""

from __future__ import annotations

from myopenclaw.context.assembler import ContextAssembler, append_hook_feedback
from myopenclaw.context.hook_feedback import HookFeedback
from myopenclaw.context.model_context import ModelContext, SystemContent, ToolDefinition
from myopenclaw.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from myopenclaw.conversations.content_blocks import TextContent, ToolCallContent
from myopenclaw.conversations.session import Session
from myopenclaw.tools.base import ToolSpec


def test_assemble_projects_windows_and_appends_hook_feedback():
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="old")]))
    session.append_assistant(AssistantMessage(content=[TextContent(text="old-a")]))
    session.append_user(UserMessage(content=[TextContent(text="new")]))
    session.append_assistant(
        AssistantMessage(
            content=[ToolCallContent(id="c1", name="read_file", arguments={"path": "a.py"})]
        )
    )
    session.append_tool_result(
        ToolResultMessage(
            tool_call_id="c1",
            tool_name="read_file",
            content=[TextContent(text="file body")],
        )
    )

    system = SystemContent.from_text("you are pickle")
    tools = [
        ToolDefinition(
            name="read_file",
            description="read a file",
            input_schema={"type": "object"},
        )
    ]
    feedback = [HookFeedback(source_event="PostToolBatch", text="hook note")]

    ctx = ContextAssembler().assemble(
        entries=session.active_path(),
        system=system,
        tools=tools,
        hook_feedback=feedback,
        unit_window=2,
    )

    assert isinstance(ctx, ModelContext)
    assert ctx.system is system
    assert ctx.tools == tools
    # window=2: [user new], [assistant+tool]；hook 尾部合成 user
    texts = []
    for message in ctx.messages:
        if isinstance(message, UserMessage):
            texts.append(("user", message.content[0].text))
        elif isinstance(message, AssistantMessage):
            tool_ids = [
                b.id for b in message.content if isinstance(b, ToolCallContent)
            ]
            texts.append(("assistant", tool_ids or message.content[0].text))
        elif isinstance(message, ToolResultMessage):
            texts.append(("tool", message.tool_call_id, message.content[0].text))

    assert texts[0] == ("user", "new")
    assert texts[1] == ("assistant", ["c1"])
    assert texts[2] == ("tool", "c1", "file body")
    assert texts[3] == ("user", "hook note")
    # 确认 hook 未落盘：session path 无 hook 文本
    assert all(
        "hook note" not in str(entry.payload) for entry in session.active_path()
    )


def test_append_hook_feedback_empty_is_noop():
    messages = [UserMessage(content=[TextContent(text="hi")])]
    assert append_hook_feedback(messages, []) == messages
    assert append_hook_feedback(messages, [HookFeedback(source_event="x", text="")]) == messages


def test_tool_definition_is_not_tool_spec():
    definition = ToolDefinition(name="t", description="d", input_schema={})
    spec = ToolSpec(name="t", description="d", input_schema={})
    assert type(definition) is not type(spec)
    assert not isinstance(definition, ToolSpec)
