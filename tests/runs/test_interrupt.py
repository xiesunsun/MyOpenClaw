"""中断语义：session 必须保持可继续。"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from pickel.agents.agent import Agent
from pickel.context.assembler import ContextAssembler
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    ToolResultMessage,
    agent_message_from_dict,
)
from pickel.conversations.content_blocks import TextContent, ToolCallContent
from pickel.conversations.session import Session
from pickel.conversations.session_entry import ENTRY_TYPE_MESSAGE
from pickel.hooks.lifecycle import NoopLifecycleHooks
from pickel.runs import ReActStrategy, Run
from pickel.runs.event_bus import EventBus
from pickel.runs.runtime_events import TurnInterrupted
from pickel.shared.model_config import ModelConfig
from pickel.tools.base import (
    BaseTool,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolSpec,
)
from pickel.tools.bus import ToolActivation, bus_with
from pickel.tools.shell import LocalBashOperations


class _HangingTool(BaseTool):
    """永远不返回的工具——用来模拟「中断时工具正在跑」。"""

    spec = ToolSpec(
        name="hang",
        description="Hangs forever",
        input_schema={"type": "object", "properties": {}},
    )

    async def execute(self, arguments, context) -> ToolExecutionResult:
        await asyncio.sleep(3600)
        return ToolExecutionResult(content="never")


class _ToolCallProvider:
    async def generate(self, context):
        return AssistantMessage(
            content=[ToolCallContent(id="call_1", name="hang", arguments={})],
            metadata=ModelResponseMetadata(
                provider="fake",
                model="fake-1",
                usage=ModelUsage(input_tokens=100, output_tokens=10),
            ),
        )

    async def stream(self, context):
        from pickel.providers.stream import StreamCompleted

        yield StreamCompleted(message=await self.generate(context))


class _HangingStreamProvider:
    """产 2 个文本增量后在 stream 内部永久挂起——模拟「流式生成期中断」。"""

    async def stream(self, context):
        from pickel.providers.stream import TextDelta

        yield TextDelta(text="你")
        yield TextDelta(text="好")
        await asyncio.get_running_loop().create_future()  # 挂起点在 stream 内

    async def generate(self, context):
        raise AssertionError("流式路径不应回退到 generate")


def _messages(session):
    out = []
    for entry in session.active_path():
        if entry.entry_type != ENTRY_TYPE_MESSAGE:
            continue
        try:
            out.append(agent_message_from_dict(entry.payload))
        except (KeyError, TypeError, ValueError):
            continue
    return out


class InterruptTests(unittest.IsolatedAsyncioTestCase):
    def _run(self, provider, tools):
        # bus_with 按 BUILTIN 来源注册，内置工具用裸名，故 tool_ids=["hang"] 能匹配
        bus_obj = bus_with(tools)
        return Run(
            agent=Agent(
                agent_id="Pickle",
                workspace_path=Path("."),
                behavior_path=Path("."),
                behavior_instruction="you are pickle",
                model_config=ModelConfig(provider="fake", model="fake-1"),
                tool_ids=["hang"],
            ),
            provider=provider,
            tool_bus=bus_obj,
            activation=ToolActivation(allowed=frozenset(bus_obj.list_names())),
            context_assembler=ContextAssembler(),
            lifecycle_hooks=NoopLifecycleHooks(),
            session_service=None,
            file_access_policy=None,
            workspace_files=None,
            bash_operations=LocalBashOperations(),
            unit_window=5,
            strategy=ReActStrategy(max_steps=2),
        )

    async def test_中断后不存在缺_tool_result_的_tool_call(self) -> None:
        """悬空 tool_call 会让下一轮请求被 provider 拒绝。"""
        session = Session.create(agent_id="Pickle", session_id="s1")
        run = self._run(_ToolCallProvider(), [_HangingTool()])
        bus = EventBus()

        task = asyncio.create_task(run.turn(session=session, user_text="hi", bus=bus))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)

        messages = _messages(session)
        call_ids = {
            block.id
            for message in messages
            if isinstance(message, AssistantMessage)
            for block in message.content
            if isinstance(block, ToolCallContent)
        }
        result_ids = {
            message.tool_call_id
            for message in messages
            if isinstance(message, ToolResultMessage)
        }

        self.assertTrue(call_ids, "测试前提：应该已经落盘了一条 tool_call")
        self.assertEqual(call_ids, result_ids)

    async def test_中断补齐的_tool_result_标记为错误(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="s1")
        run = self._run(_ToolCallProvider(), [_HangingTool()])

        task = asyncio.create_task(
            run.turn(session=session, user_text="hi", bus=EventBus())
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)

        results = [m for m in _messages(session) if isinstance(m, ToolResultMessage)]
        self.assertEqual(1, len(results))
        self.assertTrue(results[0].is_error)

    async def test_中断发出_turn_interrupted_事件(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="s1")
        run = self._run(_ToolCallProvider(), [_HangingTool()])
        bus = EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))

        task = asyncio.create_task(run.turn(session=session, user_text="hi", bus=bus))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)

        interrupted = [e for e in events if isinstance(e, TurnInterrupted)]
        self.assertEqual(1, len(interrupted))
        self.assertEqual(1, interrupted[0].at_step)

    async def test_中断不发_turn_completed_也不发_turn_failed(self) -> None:
        from pickel.runs.runtime_events import TurnCompleted, TurnFailed

        session = Session.create(agent_id="Pickle", session_id="s1")
        run = self._run(_ToolCallProvider(), [_HangingTool()])
        bus = EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))

        task = asyncio.create_task(run.turn(session=session, user_text="hi", bus=bus))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)

        self.assertFalse([e for e in events if isinstance(e, TurnCompleted)])
        self.assertFalse([e for e in events if isinstance(e, TurnFailed)])

    async def test_流式生成期中断也发_turn_interrupted(self) -> None:
        """stream 消费期取消不发事件的话，UI 收不到「已中断本轮」提示。"""
        from pickel.runs.runtime_events import TurnCompleted, TurnFailed

        session = Session.create(agent_id="Pickle", session_id="s1")
        run = self._run(_HangingStreamProvider(), [_HangingTool()])
        bus = EventBus()
        events = []
        bus.subscribe(lambda e: events.append(e))

        task = asyncio.create_task(run.turn(session=session, user_text="hi", bus=bus))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)

        interrupted = [e for e in events if isinstance(e, TurnInterrupted)]
        self.assertEqual(1, len(interrupted))
        self.assertEqual(1, interrupted[0].at_step)
        # 已流出的增量必须进 partial_text——中断时不能丢掉已生成的文本
        self.assertIn("你", interrupted[0].partial_text)
        self.assertIn("好", interrupted[0].partial_text)
        self.assertFalse([e for e in events if isinstance(e, TurnCompleted)])
        self.assertFalse([e for e in events if isinstance(e, TurnFailed)])

    async def test_CancelledError_必须重新抛出(self) -> None:
        """吞掉它会让 asyncio 的取消机制失效。"""
        session = Session.create(agent_id="Pickle", session_id="s1")
        run = self._run(_ToolCallProvider(), [_HangingTool()])

        task = asyncio.create_task(
            run.turn(session=session, user_text="hi", bus=EventBus())
        )
        await asyncio.sleep(0.05)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10)
        self.assertTrue(task.cancelled() or task.done())


if __name__ == "__main__":
    unittest.main()
