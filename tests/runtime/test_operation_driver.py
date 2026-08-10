from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone

from pickel.agents.agent_package import (
    AgentDefinition,
    AgentModelVersion,
    AgentPackageVersion,
    AgentRuntimeSettings,
    AgentToolVersion,
    agent_package_digest,
)
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextContent, ToolCallContent
from pickel.conversations.conversation_service import ConversationService
from pickel.hooks.decisions import (
    PostToolBatchDecision,
    PostToolUseDecision,
    PreToolUseDecision,
)
from pickel.hooks.lifecycle import LifecycleHooks
from pickel.operations.operation_service import OperationService
from pickel.persistence.in_memory_runtime_store import InMemoryRuntimeStore
from pickel.providers.base import Provider
from pickel.providers.stream import StreamCompleted
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


class _Provider(Provider):
    def __init__(self, messages: list[AssistantMessage]) -> None:
        self.messages = list(messages)
        self.requests = 0
        self.contexts = []

    @classmethod
    def from_config(cls, config):
        raise AssertionError("test provider is injected")

    async def generate(self, context):
        raise AssertionError("OperationDriver must consume stream")

    async def stream(self, context):
        self.requests += 1
        self.contexts.append(context)
        yield StreamCompleted(message=self.messages.pop(0))


class _EchoTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        description="Echo text",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )

    def __init__(self) -> None:
        self.executions = 0
        self.arguments = []

    async def execute(
        self,
        arguments: dict,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        self.executions += 1
        self.arguments.append(dict(arguments))
        return ToolExecutionResult(content=str(arguments["text"]))


class _Hooks:
    def __init__(self) -> None:
        self.completed = False

    async def pre_tool_use(self, _event):
        return PreToolUseDecision(updated_arguments={"text": "changed"})

    async def post_tool_use(self, _event):
        return PostToolUseDecision(feedback_text="tool feedback")

    async def post_tool_batch(self, _event):
        return PostToolBatchDecision(feedback_text="batch feedback")

    async def turn_end(self, _event):
        self.completed = True


def _package() -> AgentPackageVersion:
    definition = AgentDefinition(
        agent_id="Pickle",
        workspace_path="/project",
        behavior_path="/project/AGENT.md",
        skills_path=None,
        tool_ids=("echo",),
        extension_ids=(),
        file_access_mode="workspace",
        provider="anthropic",
        model="claude-test",
    )
    model = AgentModelVersion(
        provider="anthropic",
        model="claude-test",
        api_base=None,
        temperature=None,
        max_input_tokens=None,
        max_output_tokens=1024,
        provider_options={},
        required_secrets=(),
    )
    tool = AgentToolVersion(
        name="echo",
        source="builtin",
        version=None,
        origin=None,
        description="Echo text",
        input_schema=_EchoTool.spec.input_schema,
        output_schema=None,
    )
    draft = AgentPackageVersion(
        package_version_id="pending",
        digest="pending",
        agent_id="Pickle",
        definition=definition,
        behavior_instruction="Be helpful.",
        model=model,
        runtime=AgentRuntimeSettings(
            max_model_steps=4,
            context_unit_window=10,
        ),
        skills=(),
        tools=(tool,),
        created_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
    )
    digest = agent_package_digest(draft.content_dict())
    return replace(
        draft,
        package_version_id=f"agentpkg_{digest}",
        digest=digest,
    )


def test_driver_runs_default_tool_loop_through_persisted_effects() -> None:
    store = InMemoryRuntimeStore()
    store.create_conversation_session(
        session_id="session-1",
        agent_id="Pickle",
        cwd="/project",
    )
    package = _package()
    store.insert_agent_package_version(package)
    operation_service = OperationService(
        store,
        operation_id_factory=lambda: "operation-1",
        node_id_factory=lambda: "user-node",
    )
    accepted = operation_service.accept_agent_run(
        session_id="session-1",
        agent_package_version_id=package.package_version_id,
        user_message=UserMessage(content=[TextContent(text="hello")]),
    )
    provider = _Provider(
        [
            AssistantMessage(
                content=[
                    ToolCallContent(
                        id="tool-1",
                        name="echo",
                        arguments={"text": "hello"},
                    )
                ]
            ),
            AssistantMessage(content=[TextContent(text="done")]),
        ]
    )
    tool = _EchoTool()
    bus = ToolBus()
    bus.register(tool, source=ToolSource.BUILTIN)
    bindings = RuntimeBindings(
        agent_package_version=package,
        provider=provider,
        tool_snapshot=bus.snapshot(ToolActivation(allowed=frozenset({"echo"}))),
    )
    effects = RuntimeEffects(
        bindings=bindings,
        operation_service=operation_service,
    )
    step_ids = iter(("step-1", "step-2"))
    node_ids = iter(("assistant-1", "result-1", "assistant-2"))
    driver = OperationDriver(
        bindings=bindings,
        operation_service=operation_service,
        conversation_service=ConversationService(store),
        runtime_effects=effects,
        step_id_factory=lambda: next(step_ids),
        node_id_factory=lambda: next(node_ids),
    )

    result = asyncio.run(driver.drive_operation(accepted.operation.operation_id))

    assert result.status == "succeeded"
    assert result.assistant_message is not None
    assert result.assistant_message.content[0].text == "done"
    assert result.state.completed_step_ids == ("step-1", "step-2")
    assert provider.requests == 2
    assert tool.executions == 1
    entries = store.list_active_branch_entries(session_id="session-1")
    assert [entry.object.content["role"] for entry in entries] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]


def test_driver_persists_hook_decisions_and_routes_hooks_through_effects() -> None:
    store = InMemoryRuntimeStore()
    store.create_conversation_session(
        session_id="session-1",
        agent_id="Pickle",
        cwd="/project",
    )
    package = _package()
    store.insert_agent_package_version(package)
    operation_service = OperationService(store)
    accepted = operation_service.accept_agent_run(
        session_id="session-1",
        agent_package_version_id=package.package_version_id,
        user_message=UserMessage(content=[TextContent(text="hello")]),
    )
    provider = _Provider(
        [
            AssistantMessage(
                content=[
                    ToolCallContent(
                        id="tool-1",
                        name="echo",
                        arguments={"text": "original"},
                    )
                ]
            ),
            AssistantMessage(content=[TextContent(text="done")]),
        ]
    )
    tool = _EchoTool()
    bus = ToolBus()
    bus.register(tool, source=ToolSource.BUILTIN)
    hooks = _Hooks()
    bindings = RuntimeBindings(
        agent_package_version=package,
        provider=provider,
        tool_snapshot=bus.snapshot(ToolActivation(allowed=frozenset({"echo"}))),
        lifecycle_hooks=LifecycleHooks([hooks]),
    )
    effects = RuntimeEffects(
        bindings=bindings,
        operation_service=operation_service,
    )
    driver = OperationDriver(
        bindings=bindings,
        operation_service=operation_service,
        conversation_service=ConversationService(store),
        runtime_effects=effects,
    )

    result = asyncio.run(driver.drive_operation(accepted.operation.operation_id))

    assert result.status == "succeeded"
    assert tool.arguments == [{"text": "changed"}]
    feedback_message = provider.contexts[1].messages[-1]
    assert isinstance(feedback_message, UserMessage)
    assert feedback_message.content[0].text == "tool feedback\n\nbatch feedback"
    assert hooks.completed
