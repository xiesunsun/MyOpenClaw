from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from pickel.agents.agent import Agent
from pickel.app.runtime import RuntimeConversation
from pickel.app.runtime_models import TurnMismatchError, TurnRequest
from pickel.context.assembler import ContextAssembler
from pickel.conversations.agent_message import (
    AssistantMessage,
    UserMessage,
    agent_message_from_dict,
)
from pickel.conversations.content_blocks import TextContent, ToolCallContent
from pickel.conversations.session import Session
from pickel.conversations.session_entry import ENTRY_TYPE_MESSAGE
from pickel.hooks.lifecycle import NoopLifecycleHooks
from pickel.providers.stream import StreamCompleted
from pickel.runs.run import Run
from pickel.runs.runtime_events import (
    PendingInputDelivered,
    TurnStarted,
)
from pickel.runs.strategy.react import ReActStrategy
from pickel.shared.model_config import ModelConfig
from pickel.tools.base import BaseTool, ToolExecutionResult, ToolSpec
from pickel.tools.bus import ToolActivation, bus_with
from pickel.tools.shell import LocalBashOperations


def _message(text: str) -> UserMessage:
    return UserMessage(content=[TextContent(text=text)])


def _request(text: str) -> TurnRequest:
    return TurnRequest(message=_message(text))


class _Hooks(NoopLifecycleHooks):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[tuple[str, str]] = []

    async def user_prompt_submit(self, event):
        self.sources.append((event.source, event.prompt))
        return await super().user_prompt_submit(event)


class _BlockingProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()

    async def stream(self, context):
        self.calls += 1
        if self.calls == 1:
            self.first_started.set()
            await self.release_first.wait()
        yield StreamCompleted(
            message=AssistantMessage(content=[TextContent(text=f"reply-{self.calls}")])
        )

    async def generate(self, context):
        raise AssertionError("测试应走流式路径")


class _ToolProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, context):
        self.calls += 1
        content = (
            [ToolCallContent(id="call-1", name="wait", arguments={})]
            if self.calls == 1
            else [TextContent(text="done")]
        )
        yield StreamCompleted(message=AssistantMessage(content=content))

    async def generate(self, context):
        raise AssertionError("测试应走流式路径")


class _BlockingTool(BaseTool):
    spec = ToolSpec(
        name="wait",
        description="wait",
        input_schema={"type": "object", "properties": {}},
    )

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, arguments, context):
        self.started.set()
        await self.release.wait()
        return ToolExecutionResult(content="tool-done")


def _conversation(provider, hooks=None) -> RuntimeConversation:
    agent = Agent(
        agent_id="Pickle",
        workspace_path=Path("."),
        behavior_path=Path("."),
        behavior_instruction="test",
        model_config=ModelConfig(provider="fake", model="fake-1"),
        tool_ids=[],
    )
    tool_bus = bus_with([])
    run = Run(
        agent=agent,
        provider=provider,
        tool_bus=tool_bus,
        activation=ToolActivation(allowed=frozenset()),
        context_assembler=ContextAssembler(),
        lifecycle_hooks=hooks or NoopLifecycleHooks(),
        session_service=None,
        file_access_policy=None,
        workspace_files=None,
        bash_operations=LocalBashOperations(),
        unit_window=5,
        strategy=ReActStrategy(max_steps=4),
    )
    return RuntimeConversation(
        agent=agent,
        run=run,
        session=Session.create(agent_id=agent.agent_id),
    )


def _tool_conversation(provider, tool, hooks) -> RuntimeConversation:
    agent = Agent(
        agent_id="Pickle",
        workspace_path=Path("."),
        behavior_path=Path("."),
        behavior_instruction="test",
        model_config=ModelConfig(provider="fake", model="fake-1"),
        tool_ids=["wait"],
    )
    tool_bus = bus_with([tool])
    run = Run(
        agent=agent,
        provider=provider,
        tool_bus=tool_bus,
        activation=ToolActivation(allowed=frozenset(tool_bus.list_names())),
        context_assembler=ContextAssembler(),
        lifecycle_hooks=hooks,
        session_service=None,
        file_access_policy=None,
        workspace_files=None,
        bash_operations=LocalBashOperations(),
        unit_window=5,
        strategy=ReActStrategy(max_steps=4),
    )
    return RuntimeConversation(
        agent=agent,
        run=run,
        session=Session.create(agent_id=agent.agent_id),
    )


class RuntimeInputControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_steering_waits_until_current_tool_batch_finishes(self) -> None:
        provider = _ToolProvider()
        tool = _BlockingTool()
        hooks = _Hooks()
        conversation = _tool_conversation(provider, tool, hooks)
        events = []
        conversation.subscribe(events.append)

        task = asyncio.create_task(conversation.turn(_request("initial")))
        await tool.started.wait()
        turn_id = conversation.active_turn_id
        assert turn_id is not None
        await conversation.steer(_request("redirect"), expected_turn_id=turn_id)

        self.assertEqual([("initial", "initial")], hooks.sources)
        tool.release.set()
        await task

        messages = [
            agent_message_from_dict(entry.payload)
            for entry in conversation.session.active_path()
            if entry.entry_type == ENTRY_TYPE_MESSAGE
        ]
        roles = [message.role for message in messages]
        self.assertEqual(
            ["user", "assistant", "tool", "user", "assistant"],
            roles,
        )
        self.assertEqual(
            [("initial", "initial"), ("steer", "redirect")],
            hooks.sources,
        )

    async def test_steer_is_delivered_inside_current_turn(self) -> None:
        provider = _BlockingProvider()
        hooks = _Hooks()
        conversation = _conversation(provider, hooks)
        events = []
        conversation.subscribe(events.append)

        task = asyncio.create_task(conversation.turn(_request("initial")))
        await provider.first_started.wait()
        turn_id = next(
            event.envelope.turn_id for event in events if isinstance(event, TurnStarted)
        )
        queued = await conversation.steer(
            _request("redirect"),
            expected_turn_id=turn_id,
        )
        provider.release_first.set()
        result = await task

        self.assertEqual("completed", result.status)
        self.assertEqual(turn_id, result.turn_id)
        self.assertEqual(2, provider.calls)
        self.assertEqual(
            [("initial", "initial"), ("steer", "redirect")],
            hooks.sources,
        )
        delivered = [e for e in events if isinstance(e, PendingInputDelivered)]
        self.assertEqual([queued.input_id], [e.input_id for e in delivered])

    async def test_follow_up_starts_a_new_runtime_turn(self) -> None:
        provider = _BlockingProvider()
        hooks = _Hooks()
        conversation = _conversation(provider, hooks)
        events = []
        conversation.subscribe(events.append)

        task = asyncio.create_task(conversation.turn(_request("initial")))
        await provider.first_started.wait()
        first_turn_id = next(
            event.envelope.turn_id for event in events if isinstance(event, TurnStarted)
        )
        await conversation.follow_up(
            _request("later"),
            expected_turn_id=first_turn_id,
        )
        provider.release_first.set()
        result = await task

        turn_ids = [
            event.envelope.turn_id for event in events if isinstance(event, TurnStarted)
        ]
        self.assertEqual(2, len(turn_ids))
        self.assertNotEqual(turn_ids[0], turn_ids[1])
        self.assertEqual(turn_ids[1], result.turn_id)
        self.assertEqual(
            [("initial", "initial"), ("follow_up", "later")],
            hooks.sources,
        )

    async def test_update_cancel_and_interrupt_return_pending_inputs(self) -> None:
        provider = _BlockingProvider()
        conversation = _conversation(provider)
        events = []
        conversation.subscribe(events.append)

        task = asyncio.create_task(conversation.turn(_request("initial")))
        await provider.first_started.wait()
        turn_id = next(
            event.envelope.turn_id for event in events if isinstance(event, TurnStarted)
        )
        steering = await conversation.steer(_request("old"), expected_turn_id=turn_id)
        updated = await conversation.update_pending(
            steering.input_id,
            _request("new"),
            expected_revision=1,
        )
        follow_up = await conversation.follow_up(
            _request("later"), expected_turn_id=turn_id
        )
        await conversation.cancel_pending(
            follow_up.input_id,
            expected_revision=1,
        )

        returned = await conversation.interrupt(expected_turn_id=turn_id)
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual((updated,), returned)
        self.assertEqual((), await conversation.pending_inputs())

    async def test_stale_turn_id_is_rejected(self) -> None:
        provider = _BlockingProvider()
        conversation = _conversation(provider)
        task = asyncio.create_task(conversation.turn(_request("initial")))
        await provider.first_started.wait()

        with self.assertRaises(TurnMismatchError):
            await conversation.steer(
                _request("redirect"),
                expected_turn_id="stale",
            )

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()
