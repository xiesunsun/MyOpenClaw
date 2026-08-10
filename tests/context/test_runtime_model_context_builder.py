from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

from pickel.agents.agent_package import (
    AgentDefinition,
    AgentModelVersion,
    AgentPackageVersion,
    AgentRuntimeSettings,
    AgentSkillVersion,
    AgentToolVersion,
    agent_package_digest,
)
from pickel.context.hook_feedback import HookFeedback
from pickel.context.model_context_builder import ModelContextBuilder
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.conversation_service import ConversationService
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore


def _package() -> AgentPackageVersion:
    definition = AgentDefinition(
        agent_id="Pickle",
        workspace_path="/project",
        behavior_path="/project/AGENT.md",
        skills_path="/project/skills",
        tool_ids=("echo",),
        extension_ids=(),
        file_access_mode="workspace",
        provider="anthropic",
        model="claude-test",
    )
    draft = AgentPackageVersion(
        package_version_id="pending",
        digest="pending",
        agent_id="Pickle",
        definition=definition,
        behavior_instruction="Frozen behavior.",
        model=AgentModelVersion(
            provider="anthropic",
            model="claude-test",
            api_base=None,
            temperature=None,
            max_input_tokens=None,
            max_output_tokens=1024,
            provider_options={},
            required_secrets=(),
        ),
        runtime=AgentRuntimeSettings(
            max_model_steps=8,
            context_unit_window=2,
        ),
        skills=(
            AgentSkillVersion(
                name="search",
                description="Search files",
                version="1",
                status="active",
                required_env=(),
                allowed_tools=("echo",),
                source_path="/snapshot/search/SKILL.md",
                content="frozen skill",
                digest="a" * 64,
            ),
        ),
        tools=(
            AgentToolVersion(
                name="echo",
                source="builtin",
                version=None,
                origin=None,
                description="Echo text",
                input_schema={"type": "object"},
                output_schema=None,
            ),
        ),
        created_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
    )
    digest = agent_package_digest(draft.content_dict())
    return replace(
        draft,
        package_version_id=f"agentpkg_{digest}",
        digest=digest,
    )


def _entries():
    store = InMemoryRuntimeStore()
    service = ConversationService(
        store,
        session_id_factory=lambda: "session-1",
    )
    service.create_conversation_session(agent_id="Pickle", cwd="/project")
    service.append_user_message(
        session_id="session-1",
        message=UserMessage(content=[TextContent(text="old")]),
    )
    service.append_user_message(
        session_id="session-1",
        message=UserMessage(content=[TextContent(text="latest")]),
    )
    return service.list_active_branch_entries(session_id="session-1")


def test_builder_consumes_frozen_package_and_conversation_entries() -> None:
    context = asyncio.run(
        ModelContextBuilder().build_model_context(
            agent_package_version=_package(),
            conversation_entries=_entries(),
            session_id="session-1",
            hook_feedback=(HookFeedback(source_event="test", text="feedback"),),
        )
    )

    assert context.system.sections[0].text == "Frozen behavior."
    assert "/snapshot/search/SKILL.md" in context.system.as_text()
    assert [tool.name for tool in context.tools] == ["echo"]
    assert [message.content[0].text for message in context.messages] == [
        "old",
        "latest",
        "feedback",
    ]


def test_builder_passes_stable_session_identity_to_recall() -> None:
    seen = []

    class _Recall:
        async def provide(self, *, session_id: str, current_user_text: str = ""):
            seen.append((session_id, current_user_text))
            return [UserMessage(content=[TextContent(text="recalled")])]

    context = asyncio.run(
        ModelContextBuilder().build_model_context(
            agent_package_version=_package(),
            conversation_entries=_entries(),
            session_id="session-1",
            recall_sources=(_Recall(),),
        )
    )

    assert seen == [("session-1", "latest")]
    assert context.messages[-1].content[0].text == "recalled"
