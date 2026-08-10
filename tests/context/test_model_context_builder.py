"""ModelContextBuilder：模型上下文的唯一构建路径。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from pickel.agents.agent import Agent
from pickel.agents.skills import SkillManifest, compose_system_instruction_parts
from pickel.context.hook_feedback import HookFeedback
from pickel.context.model_context import ModelContext
from pickel.context.hook_feedback import append_hook_feedback
from pickel.runs.legacy_model_context_builder import (
    LegacyModelContextBuilder,
    build_tool_definitions,
)
from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextContent, ToolCallContent
from pickel.conversations.session import Session
from pickel.shared.model_config import ModelConfig
from pickel.tools.base import (
    BaseTool,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolSpec,
)
from pickel.tools.bus import ToolActivation, ToolBus, ToolSource, bus_with


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


def _run(*, agent: Agent | None = None, unit_window: int = 5):
    return SimpleNamespace(
        agent=agent or _agent(),
        unit_window=unit_window,
    )


def _snapshot(*tools):
    """建一份只含给定工具的快照；不传则默认 _EchoTool。"""
    bus = bus_with(list(tools) or [_EchoTool()])
    return bus.snapshot(ToolActivation(allowed=frozenset(bus.list_names())))


def _build_model_context(**kwargs):
    return LegacyModelContextBuilder().build_model_context(**kwargs)


def test_build_model_context_includes_system_history_feedback_and_tools():
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

    ctx = asyncio.run(
        _build_model_context(
            run=run,
            session=session,
            hook_feedback=feedback,
            unit_window=2,
            tool_snapshot=_snapshot(),
        )
    )

    assert isinstance(ctx, ModelContext)
    assert ctx.system.as_text() == "You are Pickle."
    assert [t.name for t in ctx.tools] == ["echo"]
    texts = []
    for message in ctx.messages:
        if isinstance(message, UserMessage):
            texts.append(("user", message.content[0].text))
        elif isinstance(message, AssistantMessage):
            tool_ids = [b.id for b in message.content if isinstance(b, ToolCallContent)]
            texts.append(("assistant", tool_ids or message.content[0].text))
        elif isinstance(message, ToolResultMessage):
            texts.append(("tool", message.tool_call_id, message.content[0].text))

    assert texts[0] == ("user", "new")
    assert texts[1] == ("assistant", ["c1"])
    assert texts[2] == ("tool", "c1", "x")
    assert texts[3] == ("user", "hook note")


def _skill(name: str) -> "SkillManifest":
    return SkillManifest(
        name=name,
        description=f"{name} description",
        skill_dir=Path(f"/tmp/pickle/skills/{name}"),
        skill_file=Path(f"/tmp/pickle/skills/{name}/SKILL.md"),
    )


def test_build_model_context_splits_system_sections_without_skills():
    """无 skills 时只有 behavior 一段。"""
    session = Session.create(agent_id="Pickle")
    ctx = asyncio.run(_build_model_context(run=_run(), session=session))

    assert [s.name for s in ctx.system.sections] == ["behavior"]


def test_build_model_context_splits_system_sections_with_skills():
    """有 skills 时拆为 behavior / skills_guidance / skills_catalog 三段。"""
    session = Session.create(agent_id="Pickle")
    run = _run(agent=_agent(skills=[_skill("alpha"), _skill("beta")]))

    ctx = asyncio.run(_build_model_context(run=run, session=session))

    assert [s.name for s in ctx.system.sections] == [
        "behavior",
        "skills_guidance",
        "skills_catalog",
    ]
    assert ctx.system.sections[0].text == "You are Pickle."
    assert "alpha" in ctx.system.sections[2].text
    assert "beta" in ctx.system.sections[2].text


def test_build_model_context_preserves_full_system_instruction():
    """分段后 provider 收到的 system 文本逐字节不变。"""
    session = Session.create(agent_id="Pickle")

    for skills in ([], [_skill("alpha")], [_skill("alpha"), _skill("beta")]):
        for behavior in ("You are Pickle.", ""):
            agent = _agent(behavior_instruction=behavior, skills=skills)
            expected = compose_system_instruction_parts(
                behavior, skills
            ).full_instruction

            ctx = asyncio.run(
                _build_model_context(run=_run(agent=agent), session=session)
            )

            assert ctx.system.as_text() == expected


def test_build_model_context_skips_empty_behavior_section():
    """behavior 为空时不产生空 section。"""
    session = Session.create(agent_id="Pickle")
    run = _run(agent=_agent(behavior_instruction="", skills=[_skill("alpha")]))

    ctx = asyncio.run(_build_model_context(run=run, session=session))

    assert [s.name for s in ctx.system.sections] == [
        "skills_guidance",
        "skills_catalog",
    ]


def test_build_model_context_appends_recall_messages():
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="hello")]))

    class _FakeRecall:
        async def provide(self, *, session_id, current_user_text: str = ""):
            return [UserMessage(content=[TextContent(text="recalled")])]

    ctx = asyncio.run(
        _build_model_context(
            run=_run(),
            session=session,
            recall_sources=[_FakeRecall()],
        )
    )
    assert [m.content[0].text for m in ctx.messages if isinstance(m, UserMessage)] == [
        "hello",
        "recalled",
    ]


def test_build_model_context_passes_current_user_text_to_recall():
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="query-me")]))
    seen: list[str] = []

    class _CaptureRecall:
        async def provide(self, *, session_id, current_user_text: str = ""):
            seen.append(current_user_text)
            return []

    asyncio.run(
        _build_model_context(
            run=_run(),
            session=session,
            recall_sources=[_CaptureRecall()],
        )
    )
    assert seen == ["query-me"]


def test_resolve_tools_uses_entry_name_over_spec_name():
    bus = ToolBus()
    bus.register(_EchoTool(), source=ToolSource.MCP, origin="github")
    snapshot = bus.snapshot(ToolActivation(allowed=frozenset({"mcp__github__echo"})))

    definitions = build_tool_definitions(tool_snapshot=snapshot)

    assert [d.name for d in definitions] == ["mcp__github__echo"]
    assert definitions[0].description == "Echo text"


def test_resolve_tools_returns_empty_for_missing_snapshot():
    assert build_tool_definitions(tool_snapshot=None) == []


def test_failing_recall_source_does_not_break_model_context_build():
    class _BoomRecall:
        async def provide(self, *, session_id, current_user_text=""):
            raise RuntimeError("recall exploded")

    class _HealthyRecall:
        async def provide(self, *, session_id, current_user_text=""):
            return [UserMessage(content=[TextContent(text="recalled")])]

    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="hi")]))

    context = asyncio.run(
        _build_model_context(
            run=_run(),
            session=session,
            recall_sources=[_BoomRecall(), _HealthyRecall()],
        )
    )

    # 坏的被跳过，好的仍然生效
    assert [
        message.content[0].text
        for message in context.messages
        if isinstance(message, UserMessage)
    ] == ["hi", "recalled"]


def test_append_hook_feedback_empty_is_noop():
    messages = [UserMessage(content=[TextContent(text="hi")])]
    assert append_hook_feedback(messages, []) == messages
    assert (
        append_hook_feedback(
            messages,
            [HookFeedback(source_event="x", text="")],
        )
        == messages
    )
