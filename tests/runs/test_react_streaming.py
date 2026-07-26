"""react 消费 provider.stream 并把增量转成 runtime 事件。"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import AsyncIterator

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
from pickel.providers.stream import (
    StreamCompleted,
    StreamDelta,
    TextDelta,
    ThinkingDelta,
)
from pickel.runs import ReActStrategy, Run
from pickel.runs.event_bus import EventBus
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
)
from pickel.shared.model_config import ModelConfig
from pickel.tools.bus import ToolActivation, bus_with
from pickel.tools.shell import ShellSessionManager


def _reply() -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text="你好")],
        metadata=ModelResponseMetadata(
            provider="fake",
            model="fake-1",
            usage=ModelUsage(input_tokens=100, output_tokens=10),
        ),
    )


class _StreamingProvider:
    """产出增量的 provider；generate 由 stream 实现。"""

    def __init__(self) -> None:
        self.stream_calls = 0

    async def stream(self, context: ModelContext) -> AsyncIterator[StreamDelta]:
        self.stream_calls += 1
        yield ThinkingDelta(text="想")
        yield TextDelta(text="你")
        yield TextDelta(text="好")
        yield StreamCompleted(message=_reply())

    async def generate(self, context: ModelContext) -> AssistantMessage:
        from pickel.providers.stream import accumulate

        return await accumulate(self.stream(context))


def _run(provider) -> Run:
    bus_obj = bus_with([])
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
        tool_bus=bus_obj,
        activation=ToolActivation(allowed=frozenset(bus_obj.list_names())),
        context_assembler=ContextAssembler(),
        lifecycle_hooks=NoopLifecycleHooks(),
        session_service=None,
        file_access_policy=None,
        workspace_files=None,
        shell_session_manager=ShellSessionManager(),
        unit_window=5,
        strategy=ReActStrategy(max_steps=2),
    )


class ReactStreamingTests(unittest.IsolatedAsyncioTestCase):
    async def _collect(self, provider):
        session = Session.create(agent_id="Pickle", session_id="s1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))
        await _run(provider).turn(session=session, user_text="hi", bus=bus)
        return events

    async def test_文本增量被转成_text_delta_事件(self) -> None:
        events = await self._collect(_StreamingProvider())
        texts = [e.text for e in events if isinstance(e, TextDeltaEvent)]

        self.assertEqual(["你", "好"], texts)

    async def test_思考增量被转成_thinking_delta_事件(self) -> None:
        events = await self._collect(_StreamingProvider())
        thinking = [e.text for e in events if isinstance(e, ThinkingDeltaEvent)]

        self.assertEqual(["想"], thinking)

    async def test_delta_事件在_assistant_message_之前(self) -> None:
        """增量必须先到，否则 UI 无法边生成边显示。"""
        events = await self._collect(_StreamingProvider())
        kinds = [type(e).__name__ for e in events]
        last_delta = max(
            i for i, k in enumerate(kinds) if k.endswith("DeltaEvent")
        )
        assistant = kinds.index("AssistantMessageEvent")

        self.assertLess(last_delta, assistant)

    async def test_delta_事件带完整信封(self) -> None:
        events = await self._collect(_StreamingProvider())
        deltas = [e for e in events if isinstance(e, TextDeltaEvent)]

        self.assertTrue(deltas)
        for event in deltas:
            self.assertEqual("s1", event.envelope.session_id)
            self.assertTrue(event.envelope.turn_id)
            self.assertEqual(1, event.envelope.step_index)

    async def test_seq_在_delta_之间仍然连续(self) -> None:
        events = await self._collect(_StreamingProvider())

        self.assertEqual(
            list(range(len(events))), [e.envelope.seq for e in events]
        )

    async def test_只调用一次_stream(self) -> None:
        provider = _StreamingProvider()
        await self._collect(provider)

        self.assertEqual(1, provider.stream_calls)

    async def test_非流式_provider_不产生_delta_事件(self) -> None:
        """只实现 generate 的 provider 走基类默认 stream，行为不变。"""

        class _Plain:
            async def generate(self, context):
                return _reply()

            async def stream(self, context):
                yield StreamCompleted(message=await self.generate(context))

        events = await self._collect(_Plain())
        deltas = [e for e in events if type(e).__name__.endswith("DeltaEvent")]

        self.assertEqual([], deltas)
        self.assertTrue(
            any(isinstance(e, AssistantMessageEvent) for e in events)
        )


if __name__ == "__main__":
    unittest.main()
