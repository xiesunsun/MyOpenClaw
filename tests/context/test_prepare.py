"""prepare：阶段表组装 ModelContext。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pickel.agents.agent import Agent
from pickel.context.hook_feedback import HookFeedback
from pickel.context.model_context import ModelContext
from pickel.context.prepare import prepare, resolve_recalls
from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextContent, ToolCallContent
from pickel.conversations.session import Session
from pickel.shared.model_config import ModelConfig
from pickel.tools.base import BaseTool, ToolExecutionContext, ToolExecutionResult, ToolSpec


class _EchoTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        description="Echo text",
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    )

    async def execute(
        self,
        arguments: dict[str, object],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(content=str(arguments.get("text", "")))


def _agent(**kwargs) -> Agent:
    defaults = dict(
        agent_id="Pickle",
        workspace_path=Path("/tmp/pickle"),
        behavior_path=Path("/tmp/pickle/AGENT.md"),
        behavior_instruction="You are Pickle.",
        model_config=ModelConfig(
            provider="google/gemini",
            model="gemini-3-flash-preview",
        ),
        tool_ids=["echo"],
    )
    defaults.update(kwargs)
    return Agent(**defaults)


def _run(*, agent: Agent | None = None, tools=None, unit_window: int = 5):
    return SimpleNamespace(
        agent=agent or _agent(),
        tools=tools if tools is not None else [_EchoTool()],
        unit_window=unit_window,
    )


def test_prepare_system_history_feedback_tools():
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="old")]))
    session.append_assistant(AssistantMessage(content=[TextContent(text="old-a")]))
    session.append_user(UserMessage(content=[TextContent(text="new")]))
    session.append_assistant(
        AssistantMessage(
            content=[ToolCallContent(id="c1", name="echo", arguments={"text": "x"})]
        )
    )
    session.append_tool_result(
        ToolResultMessage(
            tool_call_id="c1",
            tool_name="echo",
            content=[TextContent(text="x")],
        )
    )

    run = _run(unit_window=2)
    feedback = [HookFeedback(source_event="PostToolBatch", text="hook note")]

    ctx = prepare(
        run=run,
        session=session,
        hook_feedback=feedback,
        unit_window=2,
    )

    assert isinstance(ctx, ModelContext)
    assert ctx.system.as_text() == "You are Pickle."
    assert [t.name for t in ctx.tools] == ["echo"]
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
    assert texts[2] == ("tool", "c1", "x")
    assert texts[3] == ("user", "hook note")


def test_resolve_recalls_empty_is_noop():
    messages = [UserMessage(content=[TextContent(text="hi")])]
    run = _run()
    session = Session.create(agent_id="Pickle")
    assert (
        resolve_recalls(
            messages=messages,
            run=run,
            session=session,
            recall_sources=[],
        )
        == messages
    )


def test_prepare_with_recall_source_appends_messages():
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="hello")]))

    class _FakeRecall:
        def provide(self, *, run, session):
            return [UserMessage(content=[TextContent(text="recalled")])]

    ctx = prepare(
        run=_run(),
        session=session,
        recall_sources=[_FakeRecall()],
    )
    assert [m.content[0].text for m in ctx.messages if isinstance(m, UserMessage)] == [
        "hello",
        "recalled",
    ]
