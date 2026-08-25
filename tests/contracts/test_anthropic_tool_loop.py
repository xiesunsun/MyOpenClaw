from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pickel.agents.agent_package import (
    AgentRuntimePolicy,
    ImplementationRef,
    ModelPolicy,
    ModelVersion,
    ToolVersion,
    WorkspacePolicy,
    AgentPackageVersion,
    build_agent_package_version,
)
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.conversations.conversation_service import ConversationService
from pickel.operations.operation_service import OperationService
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore
from pickel.providers.anthropic import AnthropicProvider
from pickel.providers.base import Provider
from pickel.providers.stream import StreamCompleted
from pickel.runtime.agent import Agent
from pickel.runtime.agent_driver import AgentDriver, build_agent_inbox
from pickel.runtime.operation_driver import OperationDriver
from pickel.runtime.runtime_effects import RuntimeEffects
from pickel.tools.base import (
    BaseTool,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolSpec,
)
from pickel.tools.bus import ToolSource
from pickel.workspaces.workspace_binding import WorkspaceBinding


class _SimulatedProcessCrash(BaseException):
    pass


class _ResultTool(BaseTool):
    spec = ToolSpec(
        name="result",
        description="返回成功或失败结果",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
    )

    def __init__(self, *, crash: bool = False) -> None:
        self.crash = crash
        self.values: list[str] = []

    async def execute(
        self,
        arguments: dict,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        value = str(arguments["value"])
        self.values.append(value)
        if self.crash:
            raise _SimulatedProcessCrash
        return ToolExecutionResult(
            content=f"{value}-result",
            is_error=value == "failed",
        )


class _AnthropicContractProvider(Provider):
    artifact_service = None

    def __init__(self, replies: list[AssistantMessage]) -> None:
        self.replies = list(replies)
        self.request_messages: list[list[dict]] = []

    @classmethod
    def from_config(cls, config):
        raise AssertionError("合同测试直接注入 Provider")

    async def generate(self, context):
        raise AssertionError("运行时合同必须使用 Provider stream")

    async def stream(self, context):
        messages = AnthropicProvider._build_messages(context.messages)
        self.request_messages.append(messages)
        yield StreamCompleted(message=self.replies.pop(0))


def _package() -> AgentPackageVersion:
    return build_agent_package_version(
        agent_id="Pickle",
        format_version=1,
        behavior_instruction="You are Pickle.",
        model_policy=ModelPolicy(
            primary=ModelVersion(
                provider="anthropic",
                model="claude-test",
                api_base=None,
                temperature=None,
                max_input_tokens=None,
                max_output_tokens=1024,
                provider_options={},
                provider_implementation=ImplementationRef("provider", "anthropic"),
                required_secret_refs=(),
            )
        ),
        runtime_policy=AgentRuntimePolicy(max_model_steps=8, context_turn_window=5),
        workspace_policy=WorkspacePolicy("workspace"),
        skills=(),
        tools=(
            ToolVersion(
                name="result",
                source=ToolSource.BUILTIN,
                implementation_ref=ImplementationRef("builtin", "result"),
                version=None,
                description="返回成功或失败结果",
                input_schema={
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
                output_schema=None,
                replay_policy="never",
            ),
        ),
        extensions=(),
        created_at=datetime.now(timezone.utc),
    )


def _build_agent(
    *,
    package,
    tool: BaseTool,
    provider: Provider,
    store: SQLiteRuntimeStore,
) -> Agent:
    async def execute_tool(*, operation, state, tool_call_id, host_calls=None):
        call = next(
            item
            for item in state.current_step.tool_calls
            if item.tool_call_id == tool_call_id
        )
        context = ToolExecutionContext(
            agent_id=package.agent_id,
            session_id=operation.session_id,
            workspace_path=operation.workspace_binding.working_directory,
            operation_id=operation.operation_id,
            step_id=state.current_step.step_id,
            step_sequence=state.current_step.step_sequence,
            tool_call_id=tool_call_id,
        )
        return await tool.execute(dict(call.arguments), context)

    effects = RuntimeEffects(
        provider=provider,
        execute_tool=execute_tool,
        provider_name="anthropic",
        model_name="claude-test",
    )
    operation_service = OperationService(store)
    conversation_service = ConversationService(store)
    operation_driver = OperationDriver(
        operation_service=operation_service,
        conversation_service=conversation_service,
        package_loader=lambda package_id: package,
        effects_resolver=lambda package_id: effects,
    )
    agent_driver = AgentDriver(
        conversation_store=store,
        inbox_store=store,
        operation_service=operation_service,
        operation_driver=operation_driver,
        package_resolver=lambda session: (
            package.package_version_id,
            WorkspaceBinding(
                workspace_id=session.workspace_id,
                working_directory=session.cwd,
                allowed_root=session.cwd,
            ),
        ),
        cancel_operation=operation_service.request_cancellation,
    )
    session = conversation_service.load_conversation_session("session-1")
    return Agent(
        session_id=session.session_id,
        inbox=build_agent_inbox(session_id=session.session_id, store=store),
        driver=agent_driver,
    )


def test_anthropic_tool_results_survive_complete_runtime_and_sqlite(
    tmp_path: Path,
) -> None:
    tool = _ResultTool()
    package = _package()
    provider = _AnthropicContractProvider(
        [
            AssistantMessage(
                content=[
                    ToolCallBlock(
                        id="tool-success",
                        name="result",
                        arguments={"value": "success"},
                    ),
                    ToolCallBlock(
                        id="tool-failed",
                        name="result",
                        arguments={"value": "failed"},
                    ),
                ]
            ),
            AssistantMessage(content=[TextBlock(text="completed")]),
        ]
    )
    database_path = tmp_path / "runtime.db"
    store = SQLiteRuntimeStore(database_path)
    conversation_service = ConversationService(
        store, session_id_factory=lambda: "session-1"
    )
    conversation_service.create_conversation_session(
        agent_id="Pickle", cwd=str(tmp_path)
    )
    store.insert_agent_package_version(package)
    agent = _build_agent(
        package=package,
        tool=tool,
        provider=provider,
        store=store,
    )

    asyncio.run(
        agent.followup(UserMessage(content=(TextBlock(text="run both tools"),)))
    )
    drive_result = asyncio.run(agent.when_idle())
    result = drive_result.operation_result
    assert result is not None

    assert result.status == "succeeded"
    assert tool.values == ["success", "failed"]
    assert len(provider.request_messages) == 2
    second_request = provider.request_messages[1]
    assert [message["role"] for message in second_request] == [
        "user",
        "assistant",
        "user",
    ]
    tool_results = second_request[-1]["content"]
    assert [block["tool_use_id"] for block in tool_results] == [
        "tool-success",
        "tool-failed",
    ]
    assert [block.get("is_error", False) for block in tool_results] == [False, True]

    reopened_store = SQLiteRuntimeStore(database_path)
    nodes = ConversationService(reopened_store).list_active_branch_nodes(
        session_id="session-1"
    )
    assert [node.content.role for node in nodes] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "assistant",
    ]
    persisted_state = OperationService(reopened_store).load_agent_run_state(
        result.operation_id
    )
    assert persisted_state.status == "succeeded"


def test_unknown_tool_effect_is_not_replayed_after_process_restart(
    tmp_path: Path,
) -> None:
    crashing_tool = _ResultTool(crash=True)
    package = _package()
    database_path = tmp_path / "runtime.db"
    store = SQLiteRuntimeStore(database_path)
    conversation_service = ConversationService(
        store, session_id_factory=lambda: "session-1"
    )
    conversation_service.create_conversation_session(
        agent_id="Pickle", cwd=str(tmp_path)
    )
    store.insert_agent_package_version(package)
    provider = _AnthropicContractProvider(
        [
            AssistantMessage(
                content=[
                    ToolCallBlock(
                        id="external-effect",
                        name="result",
                        arguments={"value": "started"},
                    )
                ]
            )
        ]
    )
    agent = _build_agent(
        package=package,
        tool=crashing_tool,
        provider=provider,
        store=store,
    )

    with pytest.raises(_SimulatedProcessCrash):
        asyncio.run(
            agent.followup(
                UserMessage(content=(TextBlock(text="start external effect"),))
            )
        )
        asyncio.run(agent.when_idle())

    operation_service = OperationService(store)
    operation = operation_service.list_operations(session_id="session-1")[0]
    crashed_state = operation_service.load_agent_run_state(operation.operation_id)
    assert crashed_state.current_step is not None
    assert crashed_state.current_step.tool_calls[0].status == "intent_recorded"

    replacement_tool = _ResultTool()
    replacement_provider = _AnthropicContractProvider(
        [AssistantMessage(content=[TextBlock(text="must not be requested")])]
    )
    reopened_store = SQLiteRuntimeStore(database_path)
    recovered_agent = _build_agent(
        package=package,
        tool=replacement_tool,
        provider=replacement_provider,
        store=reopened_store,
    )

    result = asyncio.run(recovered_agent.when_idle())

    assert result.operation_result is not None
    assert result.operation_result.status == "waiting"
    assert result.operation_result.state.status == "waiting"
    assert replacement_tool.values == []
    assert replacement_provider.request_messages == []
