from __future__ import annotations

import asyncio

from pickel.hooks.decisions import PreToolUseDecision, UserPromptSubmitDecision
from pickel.hooks.events import PreToolUseEvent, UserPromptSubmitEvent
from pickel.hooks.lifecycle import LifecycleHooks


def test_user_input_block_takes_precedence() -> None:
    class Allow:
        async def user_prompt_submit(self, _event):
            return UserPromptSubmitDecision(action="continue")

    class Block:
        async def user_prompt_submit(self, _event):
            return UserPromptSubmitDecision(action="block", reason="denied")

    decision = asyncio.run(
        LifecycleHooks([Allow(), Block()]).user_prompt_submit(
            UserPromptSubmitEvent(prompt="hello")
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
            PreToolUseEvent(arguments={"original": True})
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
            PreToolUseEvent(tool_name="echo", arguments={})
        )
    )

    assert decision.action == "deny"
