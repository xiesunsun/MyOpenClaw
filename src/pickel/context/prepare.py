"""prepare：模型请求组装的唯一编排入口。

阶段顺序：system → history → recalls → feedback → tools。
不调用 before_request（P2）。
"""

from __future__ import annotations

from typing import Any, Sequence

from pickel.context.assembler import append_hook_feedback
from pickel.context.hook_feedback import HookFeedback
from pickel.context.model_context import ModelContext, SystemContent, ToolDefinition
from pickel.context.projection import project_messages
from pickel.context.recall import Recall
from pickel.context.window import apply_window
from pickel.conversations.agent_message import AgentMessage


def resolve_system(*, run: Any) -> SystemContent:
    """behavior + templates + skills catalog。

    skills_path 非空时每次 prepare 重新 discover；否则用 agent.skills。
    """
    # 惰性导入：避免 agents.skills → context 包 → prepare 循环
    from pickel.agents.skills import SkillRegistry, compose_system_instruction_parts

    agent = run.agent
    if agent.skills_path is not None:
        skills = SkillRegistry.discover(agent.skills_path)
    else:
        skills = list(agent.skills)
    parts = compose_system_instruction_parts(agent.behavior_instruction, skills)
    return SystemContent.from_text(parts.full_instruction)


def resolve_history(*, session: Any, unit_window: int) -> list[AgentMessage]:
    """projection + window。"""
    messages = project_messages(session.active_path())
    return apply_window(messages, unit_window=unit_window)


def resolve_recalls(
    *,
    messages: list[AgentMessage],
    run: Any,
    session: Any,
    recall_sources: Sequence[Recall],
) -> list[AgentMessage]:
    """将各 Recall.provide 结果追加到消息列表。"""
    if not recall_sources:
        return list(messages)
    result = list(messages)
    for source in recall_sources:
        result.extend(source.provide(run=run, session=session))
    return result


def resolve_feedback(
    *,
    messages: list[AgentMessage],
    hook_feedback: list[HookFeedback] | None,
) -> list[AgentMessage]:
    """hook 文本 → 尾部 user（不落库）。"""
    return append_hook_feedback(messages, hook_feedback or [])


def resolve_tools(*, run: Any) -> list[ToolDefinition]:
    """run.tools → ToolDefinition 列表。"""
    return [
        ToolDefinition(
            name=tool.spec.name,
            description=tool.spec.description,
            input_schema=tool.spec.input_schema,
        )
        for tool in run.tools
    ]


def prepare(
    *,
    run: Any,
    session: Any,
    hook_feedback: list[HookFeedback] | None = None,
    unit_window: int | None = None,
    recall_sources: Sequence[Recall] | None = None,
) -> ModelContext:
    """组装一次模型调用入参（ModelContext / Request）。"""
    window = run.unit_window if unit_window is None else unit_window
    system = resolve_system(run=run)
    messages = resolve_history(session=session, unit_window=window)
    messages = resolve_recalls(
        messages=messages,
        run=run,
        session=session,
        recall_sources=recall_sources or [],
    )
    messages = resolve_feedback(messages=messages, hook_feedback=hook_feedback)
    tools = resolve_tools(run=run)
    return ModelContext(system=system, messages=messages, tools=tools)
