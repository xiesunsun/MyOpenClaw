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
from pickel.operations.active_plan import ActivePlan, PlanItem
from pickel.context.multi_agent_guidance import MULTI_AGENT_GUIDANCE
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_service import ConversationService
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.tools.bus import ToolSource
from pickel.tools.catalog import builtin_tools


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
                output_schema={"type": "string"},
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


def test_builder_appends_active_plan_once_at_message_tail(tmp_path) -> None:
    context = ModelContextBuilder().build_model_context(
        package=_package(),
        visible_messages=_visible_messages(tmp_path),
        contributions=ContextContributions(
            messages=(UserMessage(content=(TextBlock(text="hook"),)),)
        ),
        active_plan=ActivePlan(
            items=(
                PlanItem("done", "completed"),
                PlanItem("now", "in_progress"),
                PlanItem("later", "pending"),
            )
        ),
    )

    assert context.messages[-1].content[0].text == (
        "<active_plan>\n\n# Work Plan\n\n"
        "- [x] done\n- [~] now\n- [ ] later\n\n</active_plan>"
    )


def test_builder_adds_work_plan_guidance_only_for_update_plan_package() -> None:
    package = _package()
    with_update_plan = build_agent_package_version(
        agent_id=package.agent_id,
        format_version=package.format_version,
        behavior_instruction=package.behavior_instruction,
        model_policy=package.model_policy,
        runtime_policy=package.runtime_policy,
        workspace_policy=package.workspace_policy,
        skills=package.skills,
        tools=package.tools
        + (
            ToolVersion(
                name="update_plan",
                source=ToolSource.BUILTIN,
                implementation_ref=ImplementationRef("builtin", "update_plan"),
                version=None,
                description="Update work plan",
                input_schema={"type": "object"},
                output_schema={"type": "object"},
                replay_policy="safe",
            ),
        ),
        extensions=package.extensions,
        created_at=package.created_at,
    )

    context = ModelContextBuilder().build_model_context(
        package=with_update_plan, visible_messages=()
    )
    assert any(
        section.name == "work_plan_guidance" for section in context.system.sections
    )
    assert not any(
        section.name == "work_plan_guidance"
        for section in ModelContextBuilder()
        .build_model_context(package=package, visible_messages=())
        .system.sections
    )


def test_builder_always_exposes_stable_multi_agent_lifecycle_guidance(tmp_path) -> None:
    context = ModelContextBuilder().build_model_context(
        package=_package(),
        visible_messages=_visible_messages(tmp_path),
    )

    guidance = next(
        section.text
        for section in context.system.sections
        if section.name == "multi_agent_guidance"
    )
    assert guidance == MULTI_AGENT_GUIDANCE
    assert "child runs independently" in guidance
    assert "automatically delivers its terminal result" in guidance
    assert "finish the current Operation normally" in guidance
    assert "Never use `bash` sleep, files, or `list_agents`" in guidance
    assert "`send_message` sends a follow-up from a Parent" in guidance
    assert "`report` sends an intermediate message from a Child" in guidance


def test_builder_exposes_multi_agent_tool_contracts_to_model(tmp_path) -> None:
    base_package = _package()
    package = build_agent_package_version(
        agent_id=base_package.agent_id,
        format_version=base_package.format_version,
        behavior_instruction=base_package.behavior_instruction,
        model_policy=base_package.model_policy,
        runtime_policy=base_package.runtime_policy,
        workspace_policy=base_package.workspace_policy,
        skills=base_package.skills,
        tools=tuple(
            ToolVersion(
                name=tool.spec.name,
                source=ToolSource.BUILTIN,
                implementation_ref=ImplementationRef("builtin", tool.spec.name),
                version=None,
                description=tool.spec.description,
                input_schema=tool.spec.input_schema,
                output_schema=tool.spec.output_schema,
                replay_policy=tool.spec.replay_policy,
            )
            for tool in builtin_tools()
            if tool.spec.name
            in {"delegate_agent", "send_message", "list_agents", "report"}
        ),
        extensions=base_package.extensions,
        created_at=base_package.created_at,
        delegation_policy=base_package.delegation_policy,
    )

    context = ModelContextBuilder().build_model_context(
        package=package,
        visible_messages=_visible_messages(tmp_path),
    )
    tools = {tool.name: tool for tool in context.tools}
    assert (
        "automatically delivered to this Parent" in tools["delegate_agent"].description
    )
    assert "from this Parent" in tools["send_message"].description
    assert "must not be used to poll" in tools["list_agents"].description
    assert "from this Child" in tools["report"].description
    assert "prompt" in tools["delegate_agent"].input_schema["properties"]
    assert tools["list_agents"].output_schema["items"]["properties"]["status"]["enum"]
