"""turn 级事件：started / completed / failed。"""

from __future__ import annotations

import unittest
from pathlib import Path

from pickel.agents.agent import Agent
from pickel.context.assembler import ContextAssembler
from pickel.context.model_context import ModelContext
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
)
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.session import Session
from pickel.hooks.lifecycle import NoopLifecycleHooks
from pickel.providers.base import Provider
from pickel.runs import ReActStrategy, Run
from pickel.runs.event_bus import EventBus
from pickel.runs.runtime_events import TurnCompleted, TurnFailed, TurnStarted
from pickel.tools.bus import ToolActivation, bus_with
from pickel.shared.model_config import ModelConfig
from pickel.tools.shell import LocalBashOperations


class _Provider(Provider):
    def __init__(self, reply=None, error=None):
        self.reply = reply
        self.error = error

    @classmethod
    def from_config(cls, config: ModelConfig) -> "_Provider":
        raise NotImplementedError

    async def generate(self, context: ModelContext) -> AssistantMessage:
        if self.error is not None:
            raise self.error
        return self.reply


def _reply() -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text="done")],
        metadata=ModelResponseMetadata(
            provider="fake",
            model="fake-1",
            usage=ModelUsage(input_tokens=100, output_tokens=10),
        ),
    )


def _run(provider) -> Run:
    return Run(
        agent=Agent(
            agent_id="Pickle",
            workspace_path=Path("."),
            behavior_path=Path("."),
            behavior_instruction="you are pickle",
            model_config=ModelConfig(provider="fake", model="fake-1"),
            tool_ids=[],
        ),
        provider=provider,
        tool_bus=(_bus := bus_with([])),
        activation=ToolActivation(allowed=frozenset(_bus.list_names())),
        context_assembler=ContextAssembler(),
        lifecycle_hooks=NoopLifecycleHooks(),
        session_service=None,
        file_access_policy=None,
        workspace_files=None,
        bash_operations=LocalBashOperations(),
        unit_window=5,
        strategy=ReActStrategy(max_steps=2),
    )


class TurnEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_turn_started_是第一个事件且带_user_text(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="s1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))

        await _run(_Provider(reply=_reply())).turn(
            session=session, user_text="hello", bus=bus
        )

        self.assertIsInstance(events[0], TurnStarted)
        self.assertEqual("hello", events[0].user_text)
        self.assertEqual(0, events[0].envelope.seq)

    async def test_turn_completed_是最后一个事件且带_usage(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="s1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))

        await _run(_Provider(reply=_reply())).turn(
            session=session, user_text="hello", bus=bus
        )

        self.assertIsInstance(events[-1], TurnCompleted)
        self.assertEqual(1, events[-1].usage.steps)
        self.assertEqual(100, events[-1].usage.input_tokens)
        self.assertGreaterEqual(events[-1].elapsed_ms, 0)

    async def test_同一个_turn_的所有事件共享_turn_id(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="s1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))

        await _run(_Provider(reply=_reply())).turn(
            session=session, user_text="hello", bus=bus
        )

        turn_ids = {e.envelope.turn_id for e in events}
        self.assertEqual(1, len(turn_ids))
        self.assertTrue(next(iter(turn_ids)))

    async def test_provider_抛异常时发_turn_failed_并重新抛出(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="s1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))

        with self.assertRaises(ValueError):
            await _run(_Provider(error=ValueError("boom"))).turn(
                session=session, user_text="hello", bus=bus
            )

        failed = [e for e in events if isinstance(e, TurnFailed)]
        self.assertEqual(1, len(failed))
        self.assertEqual("ValueError", failed[0].error_type)
        self.assertIn("boom", failed[0].message)
        self.assertIn("ValueError", failed[0].traceback_text)

    async def test_失败时不发_turn_completed(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="s1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))

        with self.assertRaises(ValueError):
            await _run(_Provider(error=ValueError("boom"))).turn(
                session=session, user_text="hello", bus=bus
            )

        self.assertFalse([e for e in events if isinstance(e, TurnCompleted)])

    async def test_hook_阻断时不发_turn_failed(self) -> None:
        """阻断是正常结果，不是错误。"""
        from pickel.hooks.decisions import UserPromptSubmitDecision

        class BlockingHooks(NoopLifecycleHooks):
            async def user_prompt_submit(self, event):
                return UserPromptSubmitDecision(action="block", reason="nope")

        run = _run(_Provider(reply=_reply()))
        run.lifecycle_hooks = BlockingHooks()
        session = Session.create(agent_id="Pickle", session_id="s1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))

        await run.turn(session=session, user_text="hello", bus=bus)

        self.assertFalse([e for e in events if isinstance(e, TurnFailed)])

    async def test_hook_阻断轮的_turn_completed_usage_为_None_不泄漏上一轮(
        self,
    ) -> None:
        """阻断轮没发生任何模型调用，usage 必须是 None。

        若照搬成功路径去 last_turn_usage(session)，Session 里还留着上一轮的
        assistant，阻断轮会把上一轮用量重报一遍。
        """
        from pickel.hooks.decisions import UserPromptSubmitDecision

        class BlockSecondTurnHooks(NoopLifecycleHooks):
            def __init__(self) -> None:
                super().__init__()
                self.calls = 0

            async def user_prompt_submit(self, event):
                self.calls += 1
                if self.calls == 1:
                    return UserPromptSubmitDecision()
                return UserPromptSubmitDecision(action="block", reason="nope")

        run = _run(_Provider(reply=_reply()))
        run.lifecycle_hooks = BlockSecondTurnHooks()
        session = Session.create(agent_id="Pickle", session_id="s1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))

        await run.turn(session=session, user_text="hello", bus=bus)
        completed = [e for e in events if isinstance(e, TurnCompleted)]
        self.assertEqual(1, completed[0].usage.steps)

        await run.turn(session=session, user_text="blocked", bus=bus)

        completed = [e for e in events if isinstance(e, TurnCompleted)]
        self.assertEqual(2, len(completed))
        self.assertIsNone(completed[1].usage)


if __name__ == "__main__":
    unittest.main()
