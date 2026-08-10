from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pickel.agents.agent_package import (
    AgentDefinition,
    AgentModelVersion,
    AgentPackageVersion,
    AgentRuntimeSettings,
    AgentToolVersion,
    agent_package_digest,
)
from pickel.context.model_context import ModelContext
from pickel.conversations.agent_message import AssistantMessage
from pickel.providers.base import Provider
from pickel.runtime.runtime_bindings import RuntimeBindingError, RuntimeBindings
from pickel.tools.base import (
    BaseTool,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolSpec,
)
from pickel.tools.bus import ToolActivation, ToolBus, ToolSource


class _Provider(Provider):
    @classmethod
    def from_config(cls, config):
        return cls()

    async def generate(self, context: ModelContext) -> AssistantMessage:
        return AssistantMessage()


class _EchoTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        description="Echo text",
        input_schema={"type": "object"},
    )

    async def execute(
        self,
        arguments: dict,
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(content="ok")


def _version(*, provider: str = "anthropic") -> AgentPackageVersion:
    definition = AgentDefinition(
        agent_id="Pickle",
        workspace_path="/project",
        behavior_path="/project/AGENT.md",
        skills_path=None,
        tool_ids=("echo",),
        extension_ids=(),
        file_access_mode="workspace",
        provider=provider,
        model="claude-test",
    )
    model = AgentModelVersion(
        provider=provider,
        model="claude-test",
        api_base=None,
        temperature=None,
        max_input_tokens=None,
        max_output_tokens=1024,
        provider_options={"timeout_seconds": 12},
        required_secrets=("api_key",),
    )
    runtime = AgentRuntimeSettings(
        max_model_steps=8,
        context_unit_window=5,
    )
    tool = AgentToolVersion(
        name="echo",
        source="builtin",
        version=None,
        origin=None,
        description="Echo text",
        input_schema={"type": "object"},
        output_schema=None,
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
        tools=(tool,),
        created_at=datetime(2026, 8, 11, tzinfo=timezone.utc),
    )
    digest = agent_package_digest(draft.content_dict())
    return replace(
        draft,
        package_version_id=f"agentpkg_{digest}",
        digest=digest,
    )


def _snapshot():
    bus = ToolBus()
    bus.register(_EchoTool(), source=ToolSource.BUILTIN)
    return bus.snapshot(ToolActivation(allowed=frozenset({"echo"})))


def test_runtime_bindings_accept_exact_anthropic_package_snapshot() -> None:
    bindings = RuntimeBindings(
        agent_package_version=_version(),
        provider=_Provider(),
        tool_snapshot=_snapshot(),
    )

    assert bindings.agent_id == "Pickle"
    assert bindings.workspace_path == Path("/project")
    assert bindings.provider_timeout_seconds == 12.0
    assert bindings.agent_package_version.runtime.max_model_steps == 8


def test_runtime_bindings_reject_non_anthropic_provider() -> None:
    with pytest.raises(RuntimeBindingError, match="Anthropic"):
        RuntimeBindings(
            agent_package_version=_version(provider="google/gemini"),
            provider=_Provider(),
            tool_snapshot=_snapshot(),
        )


def test_runtime_bindings_reject_tool_snapshot_drift() -> None:
    empty_bus = ToolBus()
    empty_snapshot = empty_bus.snapshot(ToolActivation(allowed=frozenset()))

    with pytest.raises(RuntimeBindingError, match="ToolSnapshot"):
        RuntimeBindings(
            agent_package_version=_version(),
            provider=_Provider(),
            tool_snapshot=empty_snapshot,
        )
