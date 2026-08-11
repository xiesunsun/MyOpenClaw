from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pickel.agents.agent_package import (
    AgentDefinition,
    AgentModelVersion,
    AgentPackageVersion,
    AgentRuntimeSettings,
    AgentToolVersion,
    agent_package_digest,
)
from pickel.context.model_context import ModelContext, SystemContent
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.operations.agent_run_state import (
    AgentRunState,
    ModelStepState,
    ToolCallState,
)
from pickel.providers.base import Provider
from pickel.providers.stream import StreamCompleted, TextDelta
from pickel.runtime.runtime_bindings import RuntimeBindings
from pickel.runtime.runtime_effects import (
    EffectStateNotPersistedError,
    ModelExecutionBoundaryError,
    RuntimeEffects,
)
from pickel.runtime.tool_call_executor import (
    ToolCallExecutor,
    ToolExecutionBoundaryError,
)
from pickel.tools.base import (
    BaseTool,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolSpec,
)
from pickel.tools.bus import ToolActivation, ToolBus, ToolSource


class _StreamingProvider(Provider):
    def __init__(self) -> None:
        self.requests = 0

    @classmethod
    def from_config(cls, config):
        return cls()

    async def generate(self, context: ModelContext) -> AssistantMessage:
        raise AssertionError("RuntimeEffects 应消费 stream")

    async def stream(self, context: ModelContext):
        self.requests += 1
        yield TextDelta(text="hello")
        yield StreamCompleted(
            message=AssistantMessage(content=[TextBlock(text="hello")])
        )


class _CapturingTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        description="Echo text",
        input_schema={"type": "object"},
    )

    def __init__(self) -> None:
        self.context: ToolExecutionContext | None = None

    async def execute(
        self,
        arguments: dict,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        self.context = context
        return ToolExecutionResult(content=str(arguments["text"]))


class _OperationServiceStub:
    def __init__(self, state: AgentRunState) -> None:
        self.state = state

    def load_agent_run_state(self, operation_id: str) -> AgentRunState:
        assert operation_id == self.state.operation_id
        return self.state

    def load_session_operation(self, operation_id: str):
        assert operation_id == self.state.operation_id
        return SimpleNamespace(
            operation_id=operation_id,
            session_id="session-1",
        )


def _bindings(
    *,
    provider: Provider,
    tool: BaseTool,
) -> RuntimeBindings:
    bus = ToolBus()
    bus.register(tool, source=ToolSource.BUILTIN)
    snapshot = bus.snapshot(ToolActivation(allowed=frozenset({"echo"})))
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
        required_secrets=("api_key",),
    )
    runtime = AgentRuntimeSettings(max_model_steps=8, context_unit_window=5)
    tool_version = AgentToolVersion(
        name="echo",
        source="builtin",
        version=None,
        origin=None,
        description=tool.spec.description,
        input_schema=tool.spec.input_schema,
        output_schema=tool.spec.output_schema,
    )
    draft = AgentPackageVersion(
        package_version_id="pending",
        digest="pending",
        agent_id="Pickle",
        definition=definition,
        behavior_instruction="Be helpful.",
        model=model,
        runtime=runtime,
        skills=(),
        tools=(tool_version,),
        created_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
    )
    digest = agent_package_digest(draft.content_dict())
    return RuntimeBindings(
        agent_package_version=replace(
            draft,
            package_version_id=f"agentpkg_{digest}",
            digest=digest,
        ),
        provider=provider,
        tool_snapshot=snapshot,
    )


def _state(phase: str) -> AgentRunState:
    return AgentRunState(
        operation_id="operation-1",
        revision=3,
        status="running",
        user_message_node_id="user-node",
        current_step=ModelStepState(
            step_id="step-1",
            step_sequence=1,
            phase=phase,  # type: ignore[arg-type]
        ),
    )


def _context() -> ModelContext:
    return ModelContext(system=SystemContent(), messages=[])


def test_model_effect_rejects_request_before_intent_is_recorded() -> None:
    provider = _StreamingProvider()
    bindings = _bindings(provider=provider, tool=_CapturingTool())
    state = _state("model_request_ready")
    effects = RuntimeEffects(
        bindings=bindings,
        operation_service=_OperationServiceStub(state),  # type: ignore[arg-type]
    )

    with pytest.raises(ModelExecutionBoundaryError, match="intent_recorded"):
        asyncio.run(
            effects.execute_model_request(
                state=state,
                model_context=_context(),
            )
        )

    assert provider.requests == 0


def test_recall_sources_cross_runtime_effect_boundary() -> None:
    seen = []

    class _Recall:
        async def provide(self, *, session_id: str, current_user_text: str = ""):
            seen.append((session_id, current_user_text))
            return [UserMessage(content=[TextBlock(text="recalled")])]

    bindings = replace(
        _bindings(provider=_StreamingProvider(), tool=_CapturingTool()),
        recall_sources=(_Recall(),),
    )
    effects = RuntimeEffects(
        bindings=bindings,
        operation_service=_OperationServiceStub(_state("model_request_ready")),  # type: ignore[arg-type]
    )

    messages = asyncio.run(
        effects.retrieve_recall_messages(
            session_id="session-1",
            visible_messages=[UserMessage(content=[TextBlock(text="latest")])],
        )
    )

    assert seen == [("session-1", "latest")]
    assert messages[0].content[0].text == "recalled"


def test_model_effect_streams_after_intent_and_adds_metadata() -> None:
    provider = _StreamingProvider()
    bindings = _bindings(provider=provider, tool=_CapturingTool())
    state = _state("model_request_intent_recorded")
    effects = RuntimeEffects(
        bindings=bindings,
        operation_service=_OperationServiceStub(state),  # type: ignore[arg-type]
    )
    seen = []

    result = asyncio.run(
        effects.execute_model_request(
            state=state,
            model_context=_context(),
            consume_delta=seen.append,
            context_fingerprint="digest",
            hook_injected_chars=3,
        )
    )

    assert provider.requests == 1
    assert [type(delta).__name__ for delta in seen] == [
        "TextDelta",
        "StreamCompleted",
    ]
    assert result.assistant_message.content[0].text == "hello"
    assert result.assistant_message.metadata is not None
    assert result.assistant_message.metadata.provider == "anthropic"
    assert result.assistant_message.metadata.context_fingerprint == "digest"
    assert result.assistant_message.metadata.hook_injected_chars == 3


def test_model_effect_rejects_intent_that_is_not_current_persisted_state() -> None:
    provider = _StreamingProvider()
    bindings = _bindings(provider=provider, tool=_CapturingTool())
    persisted = _state("model_request_ready")
    effects = RuntimeEffects(
        bindings=bindings,
        operation_service=_OperationServiceStub(persisted),  # type: ignore[arg-type]
    )

    with pytest.raises(EffectStateNotPersistedError, match="已持久化"):
        asyncio.run(
            effects.execute_model_request(
                state=_state("model_request_intent_recorded"),
                model_context=_context(),
            )
        )

    assert provider.requests == 0


def test_runtime_effect_executes_only_persisted_tool_intent() -> None:
    provider = _StreamingProvider()
    tool = _CapturingTool()
    bindings = _bindings(provider=provider, tool=tool)
    state = AgentRunState(
        operation_id="operation-1",
        revision=4,
        status="running",
        user_message_node_id="user-node",
        current_step=ModelStepState(
            step_id="step-1",
            step_sequence=1,
            phase="tool_calls_running",
            tool_calls=(
                ToolCallState(
                    tool_call_id="tool-1",
                    tool_name="echo",
                    arguments={"text": "hello"},
                    execution_state="intent_recorded",
                ),
            ),
        ),
    )
    effects = RuntimeEffects(
        bindings=bindings,
        operation_service=_OperationServiceStub(state),  # type: ignore[arg-type]
    )

    result = asyncio.run(effects.execute_tool_call(state=state, tool_call_id="tool-1"))

    assert result.content == "hello"
    assert tool.context is not None
    assert tool.context.operation_id == "operation-1"


def test_tool_executor_rejects_call_before_intent_is_recorded() -> None:
    executor = ToolCallExecutor(
        _bindings(provider=_StreamingProvider(), tool=_CapturingTool())
    )
    ready = ToolCallState(
        tool_call_id="tool-1",
        tool_name="echo",
        arguments={"text": "hello"},
        execution_state="ready",
    )

    with pytest.raises(ToolExecutionBoundaryError, match="intent_recorded"):
        asyncio.run(
            executor.execute_tool_call(
                tool_call=ready,
                session_id="session-1",
                operation_id="operation-1",
                step_id="step-1",
                step_sequence=1,
            )
        )


def test_tool_executor_passes_stable_operation_identity() -> None:
    tool = _CapturingTool()
    executor = ToolCallExecutor(_bindings(provider=_StreamingProvider(), tool=tool))
    intent = ToolCallState(
        tool_call_id="tool-1",
        tool_name="echo",
        arguments={"text": "hello"},
        execution_state="intent_recorded",
    )

    result = asyncio.run(
        executor.execute_tool_call(
            tool_call=intent,
            session_id="session-1",
            operation_id="operation-1",
            step_id="step-1",
            step_sequence=1,
        )
    )

    assert result.content == "hello"
    assert tool.context is not None
    assert tool.context.operation_id == "operation-1"
    assert tool.context.step_id == "step-1"
    assert tool.context.step_sequence == 1
    assert tool.context.tool_call_id == "tool-1"
