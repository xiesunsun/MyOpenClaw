"""旧 Run/ReActStrategy 切换期间使用的上下文适配器。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

from pickel.context.hook_feedback import HookFeedback, append_hook_feedback
from pickel.context.model_context import (
    ModelContext,
    SystemContent,
    SystemSection,
    ToolDefinition,
)
from pickel.context.projection import project_messages
from pickel.context.recall import Recall
from pickel.context.window import apply_window
from pickel.conversations.agent_message import AgentMessage, UserMessage
from pickel.conversations.content_blocks import TextContent
from pickel.tools.bus import ToolSnapshot

if TYPE_CHECKING:
    from pickel.conversations.session import Session
    from pickel.runs.run import Run

logger = logging.getLogger(__name__)


class LegacyModelContextBuilder:
    """适配旧 Run/Session；目标 Runtime 切换后删除。"""

    async def build_model_context(
        self,
        *,
        run: Run,
        session: Session,
        hook_feedback: list[HookFeedback] | None = None,
        unit_window: int | None = None,
        recall_sources: Sequence[Recall] | None = None,
        current_user_text: str = "",
        tool_snapshot: ToolSnapshot | None = None,
    ) -> ModelContext:
        """构建一次模型调用的完整上下文。

        ``tool_snapshot`` 由调用方在 AgentRun 开始时获取一次，同一 AgentRun
        内的所有 ModelStep 共用它，保证工具定义和实际执行对象一致。
        """
        window = run.unit_window if unit_window is None else unit_window
        system = self._build_system(run=run)
        messages = self._build_history(session=session, unit_window=window)
        messages = await self._append_recalls(
            messages=messages,
            run=run,
            session=session,
            recall_sources=recall_sources or [],
            current_user_text=current_user_text,
        )
        messages = append_hook_feedback(messages, hook_feedback or [])
        tools = build_tool_definitions(tool_snapshot=tool_snapshot)
        return ModelContext(system=system, messages=messages, tools=tools)

    @staticmethod
    def _build_system(*, run: Run) -> SystemContent:
        """按行为指令和当前技能清单构建具名 system sections。"""
        # 惰性导入：避免 agents.skills -> context 包形成导入环。
        from pickel.agents.skills import (
            SkillRegistry,
            compose_system_instruction_parts,
        )

        agent = run.agent
        if agent.skills_path is not None:
            skills = SkillRegistry.discover(agent.skills_path)
        else:
            skills = list(agent.skills)
        parts = compose_system_instruction_parts(agent.behavior_instruction, skills)
        return SystemContent(
            sections=[
                SystemSection(name=name, text=text)
                for name, text in (
                    ("behavior", parts.base_instruction),
                    ("skills_guidance", parts.skills_guidance),
                    ("skills_catalog", parts.skills_catalog),
                )
                if text
            ]
        )

    @staticmethod
    def _build_history(
        *,
        session: Session,
        unit_window: int,
    ) -> list[AgentMessage]:
        messages = project_messages(session.active_path())
        return apply_window(messages, unit_window=unit_window)

    async def _append_recalls(
        self,
        *,
        messages: list[AgentMessage],
        run: Run,
        session: Session,
        recall_sources: Sequence[Recall],
        current_user_text: str,
    ) -> list[AgentMessage]:
        """按顺序追加召回结果；单个旁路召回失败不阻断 AgentRun。"""
        if not recall_sources:
            return list(messages)

        user_text = current_user_text or self._current_user_text(session)
        result = list(messages)
        for source in recall_sources:
            try:
                provided = await source.provide(
                    session_id=session.session_id,
                    current_user_text=user_text,
                )
            except Exception:
                logger.exception("Recall source %s failed", type(source).__name__)
                continue
            result.extend(provided)
        return result

    @staticmethod
    def _current_user_text(session: Session) -> str:
        """从会话活动路径提取最后一条用户文本。"""
        for message in reversed(project_messages(session.active_path())):
            if not isinstance(message, UserMessage):
                continue
            parts = [
                block.text
                for block in message.content
                if isinstance(block, TextContent) and block.text
            ]
            if parts:
                return "\n".join(parts)
        return ""


def build_tool_definitions(
    *,
    tool_snapshot: ToolSnapshot | None,
) -> list[ToolDefinition]:
    """将运行期工具快照转换为模型可见的工具定义。"""
    if tool_snapshot is None:
        return []
    return [
        ToolDefinition(
            name=entry.name,
            description=entry.tool.spec.description,
            input_schema=entry.tool.spec.input_schema,
        )
        for entry in tool_snapshot.entries
    ]
