"""Provider-neutral ModelContext 的唯一目标态构建入口。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pickel.agents.agent_package import AgentPackageVersion, SkillVersion
from pickel.context.model_context import (
    ModelContext,
    SystemContent,
    SystemSection,
    ToolDefinition,
)
from pickel.context.multi_agent_guidance import MULTI_AGENT_GUIDANCE
from pickel.operations.active_plan import ActivePlan, render_active_plan
from pickel.templates.loader import load_templates
from pickel.conversations.agent_message import AgentMessage
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.shared.collaboration import CollaborationState

WORK_PLAN_GUIDANCE = """## Work plans

你可以使用 `update_plan` 为复杂、模糊或多阶段任务维护工作计划。

- 简单、单步任务不要创建计划。
- 多步任务应尽早创建计划，并在执行中维护状态。
- 每次调用必须提交完整计划，而不是局部补丁。
- 同时最多一个步骤处于 `in_progress`；执行时应尽量保持一个。
- 完成工作后及时把步骤设为 `completed`。
- 范围变化时可以增删、重排、改写或重新打开步骤。
- 计划不能代替实际工作；创建计划后继续执行任务。
- 所有工作完成后，把全部步骤设为 `completed`。
- 不要在普通 Assistant 文本中重复完整计划；Runtime 会展示并重新注入当前计划。"""


@dataclass(frozen=True)
class ContextContributions:
    """Recall/Hook 在 Intent 提交前产生的受限、不可变追加内容。"""

    system_sections: tuple[SystemSection, ...] = ()
    messages: tuple[AgentMessage, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "system_sections", tuple(self.system_sections))
        object.__setattr__(self, "messages", tuple(self.messages))


class ModelContextBuilder:
    """只从冻结 Package、持久化会话事实和显式旁路输入构造请求。"""

    def build_model_context(
        self,
        *,
        package: AgentPackageVersion,
        visible_messages: Sequence[AgentMessage],
        contributions: ContextContributions = ContextContributions(),
        collaboration: CollaborationState | None = None,
        active_plan: ActivePlan | None = None,
    ) -> ModelContext:
        return ModelContext(
            system=self._build_system(
                package,
                contributions=contributions,
                collaboration=collaboration,
            ),
            messages=(
                tuple(visible_messages)
                + contributions.messages
                + (
                    (
                        UserMessage(
                            content=(TextBlock(text=render_active_plan(active_plan)),)
                        ),
                    )
                    if active_plan is not None
                    else ()
                )
            ),
            tools=tuple(
                ToolDefinition(
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                    output_schema=tool.output_schema,
                )
                for tool in package.tools
            ),
        )

    def _build_system(
        self,
        version: AgentPackageVersion,
        *,
        contributions: ContextContributions,
        collaboration: CollaborationState | None,
    ) -> SystemContent:
        sections = []
        behavior = version.behavior_instruction.strip()
        if behavior:
            sections.append(SystemSection(name="behavior", text=behavior))
        sections.append(
            SystemSection(name="multi_agent_guidance", text=MULTI_AGENT_GUIDANCE)
        )
        if any(tool.name == "update_plan" for tool in version.tools):
            sections.append(
                SystemSection(name="work_plan_guidance", text=WORK_PLAN_GUIDANCE)
            )
        if version.skills:
            sections.append(
                SystemSection(
                    name="skills_guidance",
                    text=load_templates()["skills_guidance"],
                )
            )
            sections.append(
                SystemSection(
                    name="skills_catalog",
                    text=self._format_skill_catalog(version.skills),
                )
            )
        if collaboration is not None:
            collaboration_prompt = collaboration.system_prompt()
            if collaboration_prompt:
                sections.append(
                    SystemSection(
                        name="collaboration_mode",
                        text=collaboration_prompt,
                    )
                )
        sections.extend(contributions.system_sections)
        return SystemContent(sections=tuple(sections))

    def _format_skill_catalog(
        self,
        skills: Sequence[SkillVersion],
    ) -> str:
        lines = ["Available skills:"]
        for skill in skills:
            version = f" v{skill.version}" if skill.version else ""
            lines.append(f"\n## {skill.name}{version}\n{skill.description}")
            lines.append(skill.content)
        return "\n".join(lines)
