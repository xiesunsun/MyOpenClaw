from __future__ import annotations

import asyncio
from pathlib import Path

from pickel.agents.agent_package_builder import AgentPackageBuilder
from pickel.config.app_config import AppConfig
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.conversations.conversation_service import ConversationService
from pickel.operations.operation_service import OperationService
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore
from pickel.providers.anthropic import AnthropicProvider
from pickel.providers.base import Provider
from pickel.providers.stream import StreamCompleted
from pickel.runtime.agent_runtime import AgentRuntime
from pickel.runtime.operation_driver import OperationDriver
from pickel.runtime.runtime_bindings import RuntimeBindings
from pickel.runtime.runtime_effects import RuntimeEffects
from pickel.tools.base import (
    BaseTool,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolSpec,
)
from pickel.tools.bus import ToolActivation, ToolBus, ToolSource


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

    def __init__(self) -> None:
        self.values: list[str] = []

    async def execute(
        self,
        arguments: dict,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        value = str(arguments["value"])
        self.values.append(value)
        return ToolExecutionResult(
            content=f"{value}-result",
            is_error=value == "failed",
        )


class _AnthropicContractProvider(Provider):
    artifact_service = None

    def __init__(self) -> None:
        self.request_messages: list[list[dict]] = []

    @classmethod
    def from_config(cls, config):
        raise AssertionError("合同测试直接注入 Provider")

    async def generate(self, context):
        raise AssertionError("AgentRuntime 必须使用 Provider stream")

    async def stream(self, context):
        messages = AnthropicProvider._build_messages(context.messages)
        self.request_messages.append(messages)
        if len(self.request_messages) == 1:
            yield StreamCompleted(
                message=AssistantMessage(
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
                )
            )
            return
        yield StreamCompleted(
            message=AssistantMessage(content=[TextBlock(text="completed")])
        )


def _config(tmp_path: Path) -> AppConfig:
    agent_dir = tmp_path / "agents" / "Pickle"
    agent_dir.mkdir(parents=True)
    (agent_dir / "AGENT.md").write_text("You are Pickle.\n", encoding="utf-8")
    return AppConfig.model_validate(
        {
            "root": tmp_path,
            "default_agent": "Pickle",
            "default_llm": {"provider": "anthropic", "model": "claude-test"},
            "providers": {
                "anthropic": {"models": {"claude-test": {}}},
            },
            "agents": {
                "Pickle": {
                    "workspace_path": ".",
                    "behavior_path": "agents/Pickle",
                    "tools": ["result"],
                }
            },
        }
    )


def test_anthropic_tool_results_survive_complete_runtime_and_sqlite(
    tmp_path: Path,
) -> None:
    tool = _ResultTool()
    tool_bus = ToolBus()
    tool_bus.register(tool, source=ToolSource.BUILTIN)
    loaded = AgentPackageBuilder(
        app_config=_config(tmp_path),
        tool_bus=tool_bus,
    ).build_loaded_agent_package()
    provider = _AnthropicContractProvider()
    bindings = RuntimeBindings(
        agent_package_version=loaded.version,
        provider=provider,
        tool_snapshot=tool_bus.snapshot(ToolActivation(allowed=frozenset({"result"}))),
    )
    database_path = tmp_path / "runtime.db"
    store = SQLiteRuntimeStore(database_path)
    store.create_conversation_session(
        session_id="session-1",
        agent_id="Pickle",
        cwd=str(tmp_path),
    )
    store.insert_agent_package_version(loaded.version)
    operation_service = OperationService(store)
    effects = RuntimeEffects(
        bindings=bindings,
        operation_service=operation_service,
    )
    runtime = AgentRuntime(
        bindings=bindings,
        operation_service=operation_service,
        operation_driver=OperationDriver(
            bindings=bindings,
            operation_service=operation_service,
            conversation_service=ConversationService(store),
            runtime_effects=effects,
        ),
        runtime_effects=effects,
    )

    result = asyncio.run(
        runtime.start_agent_run(
            session_id="session-1",
            user_message=UserMessage(content=[TextBlock(text="run both tools")]),
        )
    )

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
    entries = reopened_store.list_active_branch_entries(session_id="session-1")
    assert [entry.object.content["role"] for entry in entries] == [
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
