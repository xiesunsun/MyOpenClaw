import asyncio
import unittest

from pickel.context.model_context import ModelContext, SystemContent
from pickel.hooks.decisions import (
    BeforeRequestDecision,
    PreToolUseDecision,
    UserPromptSubmitDecision,
    merge_before_request_decisions,
    merge_pre_tool_decisions,
    merge_user_prompt_decisions,
)
from pickel.hooks.events import (
    BeforeRequestEvent,
    PreToolUseEvent,
    UserPromptSubmitEvent,
)
from pickel.hooks.lifecycle import LifecycleHooks


class MergeRulesTests(unittest.TestCase):
    def test_user_prompt_any_block_wins(self) -> None:
        d = merge_user_prompt_decisions(
            [
                UserPromptSubmitDecision(action="continue"),
                UserPromptSubmitDecision(action="block", reason="nope"),
            ]
        )
        self.assertEqual("block", d.action)
        self.assertEqual("nope", d.reason)

    def test_pre_tool_deny_over_allow(self) -> None:
        d = merge_pre_tool_decisions(
            [
                PreToolUseDecision(action="allow"),
                PreToolUseDecision(action="deny", reason="blocked"),
            ]
        )
        self.assertEqual("deny", d.action)

    def test_ask_treated_as_deny(self) -> None:
        d = merge_pre_tool_decisions([PreToolUseDecision(action="ask")])
        self.assertEqual("deny", d.action)
        self.assertIn("确认", d.reason or "")

    def test_updated_arguments_last_wins(self) -> None:
        d = merge_pre_tool_decisions(
            [
                PreToolUseDecision(action="allow", updated_arguments={"a": 1}),
                PreToolUseDecision(action="allow", updated_arguments={"a": 2, "b": 3}),
            ]
        )
        self.assertEqual({"a": 2, "b": 3}, d.updated_arguments)

    def test_before_request_last_context_wins_and_feedback_merges(self) -> None:
        ctx1 = ModelContext(system=SystemContent.from_text("a"), messages=[])
        ctx2 = ModelContext(system=SystemContent.from_text("b"), messages=[])
        d = merge_before_request_decisions(
            [
                BeforeRequestDecision(model_context=ctx1, feedback_text="f1"),
                BeforeRequestDecision(model_context=None, feedback_text="f2"),
                BeforeRequestDecision(model_context=ctx2, feedback_text=None),
            ]
        )
        self.assertIs(ctx2, d.model_context)
        self.assertEqual("f1\nf2", d.feedback_text)


class Handler:
    def __init__(self, **responses):
        self.responses = responses
        self.calls = []

    async def user_prompt_submit(self, event):
        self.calls.append(("user_prompt_submit", event.prompt))
        return self.responses.get("user_prompt_submit")

    async def pre_tool_use(self, event):
        self.calls.append(("pre_tool_use", event.tool_name))
        return self.responses.get("pre_tool_use")

    async def post_tool_use(self, event):
        self.calls.append(("post_tool_use", event.tool_call_id))
        return self.responses.get("post_tool_use")

    async def post_tool_batch(self, event):
        self.calls.append(("post_tool_batch", len(event.outcomes)))
        return self.responses.get("post_tool_batch")

    async def before_request(self, event):
        self.calls.append(("before_request", event.step_index))
        return self.responses.get("before_request")

    async def turn_end(self, event):
        self.calls.append(("turn_end", event.reason))
        return self.responses.get("turn_end")


class BoomHandler:
    async def user_prompt_submit(self, event):
        raise RuntimeError("boom")


class LifecycleHooksTests(unittest.TestCase):
    def test_no_hooks_preserves_behavior(self) -> None:
        hooks = LifecycleHooks()
        d = asyncio.run(
            hooks.user_prompt_submit(UserPromptSubmitEvent(prompt="hi"))
        )
        self.assertEqual("continue", d.action)
        p = asyncio.run(
            hooks.pre_tool_use(
                PreToolUseEvent(tool_name="echo", tool_call_id="c1", arguments={})
            )
        )
        self.assertEqual("allow", p.action)

    def test_observer_failure_is_best_effort(self) -> None:
        hooks = LifecycleHooks(handlers=[BoomHandler()])
        d = asyncio.run(
            hooks.user_prompt_submit(UserPromptSubmitEvent(prompt="hi"))
        )
        self.assertEqual("continue", d.action)

    def test_handler_block(self) -> None:
        hooks = LifecycleHooks(
            handlers=[
                Handler(
                    user_prompt_submit=UserPromptSubmitDecision(
                        action="block", reason="x"
                    )
                )
            ]
        )
        d = asyncio.run(
            hooks.user_prompt_submit(UserPromptSubmitEvent(prompt="hi"))
        )
        self.assertEqual("block", d.action)

    def test_before_request_handler_can_replace_system(self) -> None:
        original = ModelContext(
            system=SystemContent.from_text("original"),
            messages=[],
        )
        replaced = ModelContext(
            system=SystemContent.from_text("patched-by-hook"),
            messages=[],
        )
        hooks = LifecycleHooks(
            handlers=[
                Handler(before_request=BeforeRequestDecision(model_context=replaced))
            ]
        )
        d = asyncio.run(
            hooks.before_request(
                BeforeRequestEvent(session_id="s1", model_context=original)
            )
        )
        self.assertIs(replaced, d.model_context)
        self.assertEqual("patched-by-hook", d.model_context.system.as_text())


if __name__ == "__main__":
    unittest.main()


class DenyAllTools:
    async def pre_tool_use(self, event):
        from pickel.hooks.decisions import PreToolUseDecision
        return PreToolUseDecision(action="deny", reason="denied-by-test")


class ReactHookIntegrationTests(unittest.TestCase):
    def test_pre_tool_deny_appends_synthetic_tool_result(self) -> None:
        from pathlib import Path

        from pickel.agents.agent import Agent
        from pickel.context.assembler import ContextAssembler
        from pickel.conversations.agent_message import AssistantMessage, UserMessage
        from pickel.conversations.content_blocks import TextContent, ToolCallContent
        from pickel.conversations.session import Session
        from pickel.hooks.lifecycle import LifecycleHooks
        from pickel.runs.run import Run
        from pickel.runs.strategy.react import ReActStrategy
        from pickel.shared.model_config import ModelConfig
        from pickel.tools.base import BaseTool, ToolExecutionContext, ToolExecutionResult, ToolSpec
        from pickel.tools.bus import ToolActivation, bus_with

        class FakeProvider:
            def __init__(self):
                self.calls = 0

            async def generate(self, context):
                self.calls += 1
                if self.calls == 1:
                    return AssistantMessage(
                        content=[
                            ToolCallContent(id="c1", name="echo", arguments={"text": "x"})
                        ]
                    )
                return AssistantMessage(content=[TextContent(text="after-deny")])

        class EchoTool(BaseTool):
            def __init__(self):
                self.spec = ToolSpec(
                    name="echo",
                    description="echo",
                    input_schema={"type": "object"},
                )
                self.executed = 0

            async def execute(self, arguments, context: ToolExecutionContext) -> ToolExecutionResult:
                self.executed += 1
                return ToolExecutionResult(content="should-not-run")

        agent = Agent(
            agent_id="Pickle",
            workspace_path=Path("."),
            behavior_path=Path("."),
            behavior_instruction="x",
            model_config=ModelConfig(provider="fake", model="fake"),
            tool_ids=["echo"],
        )
        tool = EchoTool()
        session = Session.create(agent_id="Pickle")
        session.append_user(UserMessage(content=[TextContent(text="hi")]))
        from pickel.tools.shell import ShellSessionManager

        bus = bus_with([tool])
        run = Run(
            agent=agent,
            provider=FakeProvider(),  # type: ignore[arg-type]
            tool_bus=bus,
            activation=ToolActivation(allowed=frozenset(bus.list_names())),
            context_assembler=ContextAssembler(),
            lifecycle_hooks=LifecycleHooks(handlers=[DenyAllTools()]),
            session_service=None,
            file_access_policy=None,
            workspace_files=None,
            shell_session_manager=ShellSessionManager(),
            unit_window=5,
            strategy=ReActStrategy(max_steps=3),
        )
        final = asyncio.run(ReActStrategy(max_steps=3).execute(run=run, session=session))
        self.assertEqual(0, tool.executed)
        roles = [e.payload.get("role") for e in session.active_path()]
        self.assertIn("tool", roles)
        tool_entry = next(e for e in session.active_path() if e.payload.get("role") == "tool")
        self.assertTrue(tool_entry.payload.get("is_error"))
        self.assertEqual("after-deny", final.content[0].text)
