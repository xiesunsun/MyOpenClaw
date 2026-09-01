from __future__ import annotations

import asyncio

from pickel.context.model_context import SystemSection
from pickel.context.model_context_builder import ContextContributions
from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.hooks.decisions import PreToolUseDecision, UserPromptSubmitDecision
from pickel.hooks.events import (
    BeforeRequestEvent,
    PreToolUseEvent,
    UserPromptSubmitEvent,
)
from pickel.hooks.lifecycle import LifecycleHooks
from pickel.shared.execution_identity import ExecutionIdentity


def test_user_input_block_takes_precedence() -> None:
    class Allow:
        async def user_prompt_submit(self, _event):
            return UserPromptSubmitDecision(action="continue")

    class Block:
        async def user_prompt_submit(self, _event):
            return UserPromptSubmitDecision(action="block", reason="denied")

    decision = asyncio.run(
        LifecycleHooks([Allow(), Block()]).user_prompt_submit(
            UserPromptSubmitEvent(
                identity=ExecutionIdentity(session_id="session-1"), prompt="hello"
            )
        )
    )

    assert decision.action == "block"
    assert decision.reason == "denied"


def test_pre_tool_hooks_form_argument_transform_chain() -> None:
    seen = []

    class AddArgument:
        def __init__(self, name, value) -> None:
            self.name = name
            self.value = value

        async def pre_tool_use(self, event):
            seen.append(dict(event.arguments))
            return PreToolUseDecision(
                updated_arguments={**event.arguments, self.name: self.value}
            )

    decision = asyncio.run(
        LifecycleHooks([AddArgument("a", 1), AddArgument("b", 2)]).pre_tool_use(
            PreToolUseEvent(
                identity=ExecutionIdentity(session_id="session-1"),
                arguments={"original": True},
            )
        )
    )

    assert seen == [{"original": True}, {"original": True, "a": 1}]
    assert decision.updated_arguments == {"original": True, "a": 1, "b": 2}


def test_pre_tool_hook_failure_denies_execution() -> None:
    class FailingHook:
        async def pre_tool_use(self, _event):
            raise RuntimeError("boom")

    decision = asyncio.run(
        LifecycleHooks([FailingHook()]).pre_tool_use(
            PreToolUseEvent(
                identity=ExecutionIdentity(session_id="session-1"),
                tool_name="echo",
                arguments={},
            )
        )
    )

    assert decision.action == "deny"


def test_before_request_hooks_only_merge_ordered_context_contributions() -> None:
    class AddContext:
        def __init__(self, name: str) -> None:
            self.name = name

        async def before_request(self, event):
            assert event.visible_messages[0].content[0].text == "visible"
            assert event.recall_messages[0].content[0].text == "recall"
            return ContextContributions(
                system_sections=(SystemSection(self.name, f"system-{self.name}"),),
                messages=(UserMessage((TextBlock(f"message-{self.name}"),)),),
            )

    event = BeforeRequestEvent(
        identity=ExecutionIdentity(session_id="session-1"),
        visible_messages=[UserMessage((TextBlock("visible"),))],
        recall_messages=[UserMessage((TextBlock("recall"),))],
    )
    contributions = asyncio.run(
        LifecycleHooks([AddContext("first"), AddContext("second")]).before_request(
            event
        )
    )

    assert isinstance(event.visible_messages, tuple)
    assert [section.name for section in contributions.system_sections] == [
        "first",
        "second",
    ]
    assert [message.content[0].text for message in contributions.messages] == [
        "message-first",
        "message-second",
    ]
