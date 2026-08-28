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
from pickel.templates.loader import load_templates
from pickel.conversations.agent_message import AgentMessage


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
    ) -> ModelContext:
        return ModelContext(
            system=self._build_system(package, contributions=contributions),
            messages=tuple(visible_messages) + contributions.messages,
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
    ) -> SystemContent:
        sections = []
        behavior = version.behavior_instruction.strip()
        if behavior:
            sections.append(SystemSection(name="behavior", text=behavior))
        sections.append(
            SystemSection(name="multi_agent_guidance", text=MULTI_AGENT_GUIDANCE)
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
