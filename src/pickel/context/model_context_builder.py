"""Provider-neutral ModelContext 的唯一目标态构建入口。"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence

from pickel.agents.agent_package import AgentPackageVersion, AgentSkillVersion
from pickel.context.hook_feedback import HookFeedback, append_hook_feedback
from pickel.context.model_context import (
    ModelContext,
    SystemContent,
    SystemSection,
    ToolDefinition,
)
from pickel.context.projection import ConversationProjector
from pickel.context.templates_loader import load_templates
from pickel.context.window import apply_window
from pickel.conversations.agent_message import AgentMessage
from pickel.conversations.conversation_node import ConversationEntry


class ModelContextBuilder:
    """只从冻结 Package、持久化会话事实和显式旁路输入构造请求。"""

    def __init__(
        self,
        projector: ConversationProjector | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._projector = projector or ConversationProjector()
        self._environ = os.environ if environ is None else environ

    def build_model_context(
        self,
        *,
        agent_package_version: AgentPackageVersion,
        conversation_entries: Sequence[ConversationEntry],
        recalled_messages: Sequence[AgentMessage] = (),
        hook_feedback: Sequence[HookFeedback] = (),
    ) -> ModelContext:
        messages = self._projector.project_conversation_messages(conversation_entries)
        messages = apply_window(
            messages,
            unit_window=agent_package_version.runtime.context_unit_window,
        )
        messages.extend(recalled_messages)
        return ModelContext(
            system=self._build_system(agent_package_version),
            messages=append_hook_feedback(messages, list(hook_feedback)),
            tools=[
                ToolDefinition(
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                )
                for tool in agent_package_version.tools
            ],
        )

    def _build_system(self, version: AgentPackageVersion) -> SystemContent:
        active_skills = [
            skill for skill in version.skills if skill.status != "archived"
        ]
        sections = []
        behavior = version.behavior_instruction.strip()
        if behavior:
            sections.append(SystemSection(name="behavior", text=behavior))
        if active_skills:
            sections.append(
                SystemSection(
                    name="skills_guidance",
                    text=load_templates()["skills_guidance"],
                )
            )
            sections.append(
                SystemSection(
                    name="skills_catalog",
                    text=self._format_skill_catalog(active_skills),
                )
            )
        return SystemContent(sections=sections)

    def _format_skill_catalog(
        self,
        skills: Sequence[AgentSkillVersion],
    ) -> str:
        lines = ["Available skills:"]
        for skill in skills:
            marks = []
            if skill.version:
                marks.append(f"v{skill.version}")
            if skill.status == "stale":
                marks.append("stale")
            missing = [
                name for name in skill.required_env if not self._environ.get(name)
            ]
            if missing:
                marks.append(f"unavailable: needs {', '.join(missing)}")
            suffix = f" ({'; '.join(marks)})" if marks else ""
            lines.append(
                f"- {skill.name}: {skill.description} "
                f"(read {skill.source_path}){suffix}"
            )
        return "\n".join(lines)
