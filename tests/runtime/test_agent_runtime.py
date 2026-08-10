from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextContent
from pickel.hooks.decisions import UserPromptSubmitDecision
from pickel.runtime.agent_runtime import AgentRunBlockedError, AgentRuntime


class _Effects:
    def __init__(self, decision: UserPromptSubmitDecision) -> None:
        self.decision = decision

    async def invoke_hook(self, hook_name, event):
        assert hook_name == "user_prompt_submit"
        assert event.prompt == "hello"
        return self.decision


class _OperationService:
    def __init__(self) -> None:
        self.arguments = None

    def accept_agent_run(self, **kwargs):
        self.arguments = kwargs
        return SimpleNamespace(operation=SimpleNamespace(operation_id="operation-1"))


def _runtime(decision: UserPromptSubmitDecision):
    service = _OperationService()
    runtime = AgentRuntime(
        bindings=SimpleNamespace(
            agent_package_version=SimpleNamespace(package_version_id="package-1")
        ),
        operation_service=service,
        operation_driver=SimpleNamespace(),
        runtime_effects=_Effects(decision),
    )
    return runtime, service


def test_agent_runtime_persists_input_hook_feedback_with_acceptance() -> None:
    runtime, service = _runtime(UserPromptSubmitDecision(feedback_text="remember this"))

    accepted = asyncio.run(
        runtime.accept_agent_run(
            session_id="session-1",
            user_message=UserMessage(content=[TextContent(text="hello")]),
        )
    )

    assert accepted.operation.operation_id == "operation-1"
    assert service.arguments["initial_model_context_feedback"] == ("remember this",)


def test_agent_runtime_does_not_accept_input_blocked_by_hook() -> None:
    runtime, service = _runtime(
        UserPromptSubmitDecision(action="block", reason="denied")
    )

    with pytest.raises(AgentRunBlockedError, match="denied"):
        asyncio.run(
            runtime.accept_agent_run(
                session_id="session-1",
                user_message=UserMessage(content=[TextContent(text="hello")]),
            )
        )

    assert service.arguments is None
