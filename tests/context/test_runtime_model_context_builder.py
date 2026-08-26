from __future__ import annotations

from datetime import datetime, timezone

from pickel.agents.agent_package import (
    AgentPackageVersion,
    AgentRuntimePolicy,
    ImplementationRef,
    ModelPolicy,
    ModelVersion,
    SkillVersion,
    ToolVersion,
    WorkspacePolicy,
    build_agent_package_version,
)
from pickel.context.model_context_builder import (
    ContextContributions,
    ModelContextBuilder,
)
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_service import ConversationService
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.tools.bus import ToolSource


def _package() -> AgentPackageVersion:
    return build_agent_package_version(
        agent_id="Pickle",
        format_version=1,
        behavior_instruction="Frozen behavior.",
        model_policy=ModelPolicy(
            primary=ModelVersion(
                provider="anthropic",
                model="claude-test",
                wire_protocol="anthropic-messages",
                api_base=None,
                temperature=None,
                max_input_tokens=None,
                max_output_tokens=1024,
                provider_options={},
                provider_implementation=ImplementationRef(
                    "provider", "anthropic-messages"
                ),
                required_secret_refs=(),
            )
        ),
        runtime_policy=AgentRuntimePolicy(max_model_steps=8, context_turn_window=2),
        workspace_policy=WorkspacePolicy("workspace"),
        skills=(
            SkillVersion(
                name="search",
                description="Search files",
                version="1",
                content="frozen skill",
                required_secret_refs=(),
                allowed_tools=("echo",),
            ),
        ),
        tools=(
            ToolVersion(
                name="echo",
                source=ToolSource.BUILTIN,
                implementation_ref=ImplementationRef("builtin", "echo"),
                version=None,
                description="Echo text",
                input_schema={"type": "object"},
                output_schema=None,
                replay_policy="safe",
            ),
        ),
        extensions=(),
        created_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
    )


def _visible_messages(tmp_path):
    store = InMemoryRuntimeStore()
    service = ConversationService(store, session_id_factory=lambda: "session-1")
    service.create_conversation_session(agent_id="Pickle", cwd=str(tmp_path))
    for text in ("old", "latest", "unwindowed"):
        service.append_user_message(
            session_id="session-1",
            message=UserMessage(content=(TextBlock(text=text),)),
        )
    from pickel.context.projection import ConversationProjector

    return ConversationProjector().project_conversation_messages(
        service.list_active_branch_nodes(session_id="session-1")
    )


def test_builder_consumes_frozen_package_and_visible_messages(tmp_path) -> None:
    context = ModelContextBuilder().build_model_context(
        package=_package(),
        visible_messages=_visible_messages(tmp_path),
        contributions=ContextContributions(
            messages=(UserMessage(content=(TextBlock(text="feedback"),)),)
        ),
    )

    assert context.system.sections[0].text == "Frozen behavior."
    assert "Available skills:" in context.system.as_text()
    assert [tool.name for tool in context.tools] == ["echo"]
    assert [message.content[0].text for message in context.messages] == [
        "old",
        "latest",
        "unwindowed",
        "feedback",
    ]


def test_builder_appends_contribution_messages(tmp_path) -> None:
    context = ModelContextBuilder().build_model_context(
        package=_package(),
        visible_messages=_visible_messages(tmp_path),
        contributions=ContextContributions(
            messages=(UserMessage(content=(TextBlock(text="recalled"),)),)
        ),
    )

    assert context.messages[-1].content[0].text == "recalled"
