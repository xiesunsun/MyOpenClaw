import asyncio
import unittest
from pathlib import Path

from pickel.agents.agent import Agent
from pickel.context.model_context_builder import ModelContextBuilder
from pickel.context.model_context import ModelContext
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
)
from pickel.conversations.content_blocks import TextContent, ToolCallContent
from pickel.conversations.session import Session
from pickel.hooks.decisions import PreToolUseDecision
from pickel.hooks.lifecycle import LifecycleHooks, NoopLifecycleHooks
from pickel.providers.base import Provider
from pickel.runs import ReActStrategy, Run
from pickel.runs.event_bus import EventBus
from pickel.runs.host_call_types import (
    CONFIRMATION_CALL,
    ConfirmationAnswer,
)
from pickel.runs.host_calls import HostCallRouter
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    RequestDigestEvent,
    StepStarted,
    ToolCallCompleted,
    ToolCallStarted,
    TurnCompleted,
)
from pickel.runs.usage_anchor import resolve_anchor
from pickel.shared.model_config import ModelConfig
from pickel.tools.base import (
    BaseTool,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolSpec,
)
from pickel.tools.bus import ToolActivation, bus_with
from pickel.tools.shell import LocalBashOperations
from tests.runs.helpers import user_message


def _assistant_text(message: AssistantMessage) -> str:
    return "\n".join(
        block.text
        for block in message.content
        if isinstance(block, TextContent) and block.text
    )


class StubProvider(Provider):
    def __init__(self, responses: list[AssistantMessage]) -> None:
        self.responses = list(responses)

    @classmethod
    def from_config(cls, config: ModelConfig) -> "StubProvider":
        raise NotImplementedError

    async def generate(self, context: ModelContext) -> AssistantMessage:
        return self.responses.pop(0)


class AlwaysToolCallProvider(Provider):
    """永远返回 tool_call，用于逼 ReAct 跑到 max_steps。"""

    def __init__(self) -> None:
        self.calls = 0

    @classmethod
    def from_config(cls, config: ModelConfig) -> "AlwaysToolCallProvider":
        raise NotImplementedError

    async def generate(self, context: ModelContext) -> AssistantMessage:
        self.calls += 1
        return AssistantMessage(
            content=[
                ToolCallContent(
                    id=f"call-{self.calls}",
                    name="echo",
                    arguments={"text": "again"},
                )
            ],
            metadata=ModelResponseMetadata(
                provider="google/gemini",
                model="gemini-3-flash-preview",
                elapsed_ms=100,
                usage=ModelUsage(input_tokens=100, output_tokens=10),
            ),
        )


class DelayEchoTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        description="Echo text",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "delay_ms": {"type": "integer"},
            },
            "required": ["text"],
        },
    )

    async def execute(
        self,
        arguments: dict[str, object],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        await asyncio.sleep(int(arguments.get("delay_ms", 0)) / 1000)
        return ToolExecutionResult(content=str(arguments["text"]))


class InvalidOutputTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        description="Return structured text",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        output_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    )

    async def execute(self, arguments, context):
        return ToolExecutionResult(
            content="bad result",
            structured_content={"text": 7},
        )


def _agent() -> Agent:
    return Agent(
        agent_id="Pickle",
        workspace_path=Path("/tmp/pickle"),
        behavior_path=Path("/tmp/pickle/AGENT.md"),
        behavior_instruction="You are Pickle.",
        model_config=ModelConfig(
            provider="google/gemini",
            model="gemini-3-flash-preview",
        ),
        tool_ids=["echo"],
    )


class RecordingHooks(LifecycleHooks):
    """记录 hook 真正看到的参数与结果。"""

    def __init__(self) -> None:
        super().__init__(handlers=[])
        self.pre_arguments: list[dict] = []
        self.post_arguments: list[dict] = []
        self.post_results: list[str] = []

    async def pre_tool_use(self, event):
        self.pre_arguments.append(dict(event.arguments))
        return await super().pre_tool_use(event)

    async def post_tool_use(self, event):
        self.post_arguments.append(dict(event.arguments))
        self.post_results.append(event.result_content)
        return await super().post_tool_use(event)


def _tool_result_texts(session: Session) -> list[str]:
    """从落盘 entry 取 tool 结果文本（不经事件）。"""
    texts: list[str] = []
    for entry in session.active_path():
        payload = entry.payload if isinstance(entry.payload, dict) else {}
        if payload.get("role") != "tool":
            continue
        for block in payload.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(block.get("text") or "")
    return texts


def _run(
    *, agent: Agent, provider: Provider, tools: list[BaseTool], strategy: ReActStrategy
) -> Run:
    return Run(
        agent=agent,
        provider=provider,
        tool_bus=(_bus := bus_with(tools)),
        activation=ToolActivation(allowed=frozenset(_bus.list_names())),
        model_context_builder=ModelContextBuilder(),
        lifecycle_hooks=NoopLifecycleHooks(),
        session_service=None,
        file_access_policy=None,
        workspace_files=None,
        bash_operations=LocalBashOperations(),
        unit_window=5,
        strategy=strategy,
    )


class RuntimeEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_output_schema_becomes_model_correctable_error(self) -> None:
        run = _run(
            agent=_agent(),
            provider=StubProvider(
                responses=[
                    AssistantMessage(
                        content=[
                            ToolCallContent(
                                id="call-1",
                                name="echo",
                                arguments={"text": "valid"},
                            )
                        ]
                    ),
                    AssistantMessage(content=[TextContent(text="done")]),
                ]
            ),
            tools=[InvalidOutputTool()],
            strategy=ReActStrategy(max_steps=2),
        )
        session = Session.create(agent_id="Pickle")

        await run.turn(session=session, user_message=user_message("hello"))

        self.assertIn("工具结果不符合 output_schema", _tool_result_texts(session)[0])

    async def test_invalid_arguments_are_rejected_before_hook_and_execution(
        self,
    ) -> None:
        hooks = RecordingHooks()
        run = _run(
            agent=_agent(),
            provider=StubProvider(
                responses=[
                    AssistantMessage(
                        content=[
                            ToolCallContent(
                                id="call-1",
                                name="echo",
                                arguments={"text": 7},
                            )
                        ]
                    ),
                    AssistantMessage(content=[TextContent(text="done")]),
                ]
            ),
            tools=[DelayEchoTool()],
            strategy=ReActStrategy(max_steps=2),
        )
        run.lifecycle_hooks = hooks
        session = Session.create(agent_id="Pickle")

        await run.turn(session=session, user_message=user_message("hello"))

        self.assertEqual([], hooks.pre_arguments)
        self.assertIn("工具参数不符合 schema", _tool_result_texts(session)[0])

    async def test_hook_modified_arguments_are_validated_again(self) -> None:
        class InvalidReplacement:
            async def pre_tool_use(self, event):
                return PreToolUseDecision(updated_arguments={"text": 7})

        run = _run(
            agent=_agent(),
            provider=StubProvider(
                responses=[
                    AssistantMessage(
                        content=[
                            ToolCallContent(
                                id="call-1",
                                name="echo",
                                arguments={"text": "valid"},
                            )
                        ]
                    ),
                    AssistantMessage(content=[TextContent(text="done")]),
                ]
            ),
            tools=[DelayEchoTool()],
            strategy=ReActStrategy(max_steps=2),
        )
        run.lifecycle_hooks = LifecycleHooks(handlers=[InvalidReplacement()])
        session = Session.create(agent_id="Pickle")

        await run.turn(session=session, user_message=user_message("hello"))

        self.assertIn("Hook 修改后的工具参数", _tool_result_texts(session)[0])

    async def test_ask_executes_only_after_confirmation_accepts(self) -> None:
        class Ask:
            async def pre_tool_use(self, event):
                return PreToolUseDecision(action="ask", reason="需要确认")

        run = _run(
            agent=_agent(),
            provider=StubProvider(
                responses=[
                    AssistantMessage(
                        content=[
                            ToolCallContent(
                                id="call-1",
                                name="echo",
                                arguments={"text": "accepted"},
                            )
                        ]
                    ),
                    AssistantMessage(content=[TextContent(text="done")]),
                ]
            ),
            tools=[DelayEchoTool()],
            strategy=ReActStrategy(max_steps=2),
        )
        run.lifecycle_hooks = LifecycleHooks(handlers=[Ask()])
        router = HostCallRouter()
        router.register(
            CONFIRMATION_CALL,
            lambda _request, _context: ConfirmationAnswer(decision="accept"),
        )
        session = Session.create(agent_id="Pickle")

        await run.turn(
            session=session,
            user_message=user_message("hello"),
            host_calls=router.client,
        )

        self.assertEqual(["accepted"], _tool_result_texts(session))

    async def test_ask_without_confirmation_handler_is_safely_denied(self) -> None:
        class Ask:
            async def pre_tool_use(self, event):
                return PreToolUseDecision(action="ask", reason="需要确认")

        run = _run(
            agent=_agent(),
            provider=StubProvider(
                responses=[
                    AssistantMessage(
                        content=[
                            ToolCallContent(
                                id="call-1",
                                name="echo",
                                arguments={"text": "must-not-run"},
                            )
                        ]
                    ),
                    AssistantMessage(content=[TextContent(text="done")]),
                ]
            ),
            tools=[DelayEchoTool()],
            strategy=ReActStrategy(max_steps=2),
        )
        run.lifecycle_hooks = LifecycleHooks(handlers=[Ask()])
        session = Session.create(agent_id="Pickle")

        await run.turn(session=session, user_message=user_message("hello"))

        self.assertEqual(
            ["工具调用未获得用户确认：需要确认"],
            _tool_result_texts(session),
        )

    async def test_tool_events_use_effective_arguments_after_hook(self) -> None:
        class ReplaceArguments:
            async def pre_tool_use(self, event):
                return PreToolUseDecision(
                    updated_arguments={"text": "hooked", "delay_ms": 0}
                )

        run = _run(
            agent=_agent(),
            provider=StubProvider(
                responses=[
                    AssistantMessage(
                        content=[
                            ToolCallContent(
                                id="call-1",
                                name="echo",
                                arguments={"text": "original"},
                            )
                        ]
                    ),
                    AssistantMessage(content=[TextContent(text="done")]),
                ]
            ),
            tools=[DelayEchoTool()],
            strategy=ReActStrategy(max_steps=3),
        )
        run.lifecycle_hooks = LifecycleHooks(handlers=[ReplaceArguments()])
        session = Session.create(agent_id="Pickle")
        bus = EventBus()
        events = []
        bus.subscribe(events.append)

        await run.turn(session=session, user_message=user_message("hello"), bus=bus)

        started = next(event for event in events if isinstance(event, ToolCallStarted))
        completed = next(
            event for event in events if isinstance(event, ToolCallCompleted)
        )
        self.assertEqual("hooked", started.tool_call.arguments["text"])
        self.assertEqual("hooked", completed.tool_call.arguments["text"])
        self.assertEqual("hooked", completed.tool_result.content)
        self.assertEqual("builtin", completed.tool_source)
        self.assertEqual("allow", completed.hook_action)
        self.assertEqual("passed", completed.validation)

    async def test_runner_emits_batch_aware_events_for_started_and_completed_calls(
        self,
    ) -> None:
        agent = Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_config=ModelConfig(
                provider="google/gemini",
                model="gemini-3-flash-preview",
            ),
            tool_ids=["echo"],
        )
        run = _run(
            agent=agent,
            provider=StubProvider(
                responses=[
                    AssistantMessage(
                        content=[
                            ToolCallContent(
                                id="call-slow",
                                name="echo",
                                arguments={"text": "slow", "delay_ms": 40},
                            ),
                            ToolCallContent(
                                id="call-fast",
                                name="echo",
                                arguments={"text": "fast", "delay_ms": 0},
                            ),
                        ]
                    ),
                    AssistantMessage(content=[TextContent(text="done")]),
                ]
            ),
            tools=[DelayEchoTool()],
            strategy=ReActStrategy(max_steps=4),
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda event: events.append(event))

        result = await run.turn(
            session=session, user_message=user_message("hello"), bus=bus
        )

        self.assertEqual("done", _assistant_text(result))
        # 工具串行执行以保留 PreToolUse 控制点
        self.assertEqual(
            [
                StepStarted,
                RequestDigestEvent,
                ToolCallStarted,
                ToolCallCompleted,
                ToolCallStarted,
                ToolCallCompleted,
                StepStarted,
                RequestDigestEvent,
                AssistantMessageEvent,
            ],
            [type(event) for event in _without_turn_events(events)],
        )
        step_events = _without_turn_events(events)
        batch_id = step_events[2].batch_id
        self.assertTrue(batch_id)
        self.assertEqual(batch_id, step_events[3].batch_id)
        self.assertEqual(batch_id, step_events[4].batch_id)
        self.assertEqual(batch_id, step_events[5].batch_id)
        self.assertEqual(0, step_events[2].call_index)
        self.assertEqual(0, step_events[3].call_index)
        self.assertEqual("slow", step_events[3].tool_result.content)
        self.assertEqual(1, step_events[4].call_index)
        self.assertEqual(1, step_events[5].call_index)
        self.assertEqual("fast", step_events[5].tool_result.content)
        self.assertEqual("done", step_events[8].text)

    async def test_runner_emits_completed_event_with_is_error_for_failing_call(
        self,
    ) -> None:
        agent = Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_config=ModelConfig(
                provider="google/gemini",
                model="gemini-3-flash-preview",
            ),
            tool_ids=["missing"],
        )
        run = _run(
            agent=agent,
            provider=StubProvider(
                responses=[
                    AssistantMessage(
                        content=[
                            ToolCallContent(
                                id="call-1",
                                name="missing",
                                arguments={},
                            )
                        ]
                    ),
                    AssistantMessage(content=[TextContent(text="done")]),
                ]
            ),
            tools=[],
            strategy=ReActStrategy(max_steps=2),
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda event: events.append(event))

        await run.turn(session=session, user_message=user_message("hello"), bus=bus)

        failure = next(
            event
            for event in events
            if isinstance(event, ToolCallCompleted) and event.tool_result.is_error
        )
        self.assertEqual("missing", failure.tool_call.name)

    async def test_每个事件都带_session_id_turn_id_与递增_seq(self) -> None:
        """信封必须一路贯通到发射点，否则事件出不了进程。"""
        agent = Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_config=ModelConfig(
                provider="google/gemini",
                model="gemini-3-flash-preview",
            ),
            tool_ids=["echo"],
        )
        run = _run(
            agent=agent,
            provider=StubProvider(
                responses=[
                    AssistantMessage(
                        content=[
                            ToolCallContent(
                                id="call-slow",
                                name="echo",
                                arguments={"text": "slow", "delay_ms": 40},
                            ),
                            ToolCallContent(
                                id="call-fast",
                                name="echo",
                                arguments={"text": "fast", "delay_ms": 0},
                            ),
                        ]
                    ),
                    AssistantMessage(content=[TextContent(text="done")]),
                ]
            ),
            tools=[DelayEchoTool()],
            strategy=ReActStrategy(max_steps=4),
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda event: events.append(event))

        await run.turn(session=session, user_message=user_message("hello"), bus=bus)

        self.assertTrue(events)
        self.assertEqual(
            list(range(len(events))),
            [e.envelope.event_sequence for e in events],
        )
        self.assertTrue(all(e.envelope.session_id == "session-1" for e in events))
        turn_ids = {e.envelope.turn_id for e in events}
        self.assertEqual(1, len(turn_ids))
        self.assertTrue(next(iter(turn_ids)))

        # 显式 turn_id：Task 5 的 turn 级事件靠它与 step 事件共享同一个 id
        explicit_run = _run(
            agent=agent,
            provider=StubProvider(
                responses=[AssistantMessage(content=[TextContent(text="done")])]
            ),
            tools=[DelayEchoTool()],
            strategy=ReActStrategy(max_steps=4),
        )
        explicit_session = Session.create(agent_id="Pickle", session_id="session-2")
        explicit_bus = EventBus()
        explicit_events = []
        explicit_bus.subscribe(lambda event: explicit_events.append(event))

        await explicit_run.strategy.execute(
            run=explicit_run,
            session=explicit_session,
            bus=explicit_bus,
            turn_id="T-1",
        )

        self.assertTrue(explicit_events)
        self.assertEqual({"T-1"}, {e.envelope.turn_id for e in explicit_events})

    async def test_max_steps_事件的_usage_不重复计入合成消息(self) -> None:
        """max_msg 复用最后一次 generate 的 metadata，落盘后再求值会数两遍。"""
        agent = Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_config=ModelConfig(
                provider="google/gemini",
                model="gemini-3-flash-preview",
            ),
            tool_ids=["echo"],
        )
        provider = AlwaysToolCallProvider()
        run = _run(
            agent=agent,
            provider=provider,
            tools=[DelayEchoTool()],
            strategy=ReActStrategy(max_steps=2),
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda event: events.append(event))

        await run.turn(session=session, user_message=user_message("hello"), bus=bus)

        self.assertEqual(2, provider.calls)
        final = [e for e in events if isinstance(e, AssistantMessageEvent)][-1]
        self.assertEqual("Reached the maximum number of reasoning steps.", final.text)
        # 真实 generate 只发生了 2 次，合成的 max_msg 不得让合计变成 3
        self.assertEqual(provider.calls, final.usage.steps)
        self.assertEqual(200, final.usage.input_tokens)
        self.assertEqual(20, final.usage.output_tokens)

    async def test_max_steps_路径上两个事件对同一_turn_给出同一份_usage(self) -> None:
        """AssistantMessageEvent 与 TurnCompleted 描述同一件事，数字必须一致。

        二者求值时机不同（一个在 strategy 内、一个在 strategy 返回后），
        只要合成消息带 usage metadata，后者就会多数一步。
        """
        agent = _agent()
        provider = AlwaysToolCallProvider()
        run = _run(
            agent=agent,
            provider=provider,
            tools=[DelayEchoTool()],
            strategy=ReActStrategy(max_steps=2),
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")
        bus = EventBus()
        events = []
        bus.subscribe(lambda event: events.append(event))

        await run.turn(session=session, user_message=user_message("hello"), bus=bus)

        assistant_event = [e for e in events if isinstance(e, AssistantMessageEvent)][
            -1
        ]
        turn_completed = [e for e in events if isinstance(e, TurnCompleted)][-1]
        self.assertEqual(assistant_event.usage, turn_completed.usage)
        self.assertEqual(2, turn_completed.usage.steps)
        self.assertEqual(200, turn_completed.usage.input_tokens)
        self.assertEqual(20, turn_completed.usage.output_tokens)

    async def test_max_steps_之后_usage_锚仍指向最后一次真实调用(self) -> None:
        """合成消息不得让 /context 的锚失效（否则每次观测都退回远程 count）。"""
        agent = _agent()
        provider = AlwaysToolCallProvider()
        run = _run(
            agent=agent,
            provider=provider,
            tools=[DelayEchoTool()],
            strategy=ReActStrategy(max_steps=2),
        )
        session = Session.create(agent_id="Pickle", session_id="session-1")

        await run.turn(session=session, user_message=user_message("hello"), bus=None)

        request = await run.model_context_builder.build_model_context(
            run=run,
            session=session,
            hook_feedback=[],
            unit_window=run.unit_window,
            recall_sources=[],
            tool_snapshot=run.tool_bus.snapshot(run.activation),
        )
        anchor = resolve_anchor(
            session=session,
            request=request,
            provider=agent.model_config.provider,
            model=agent.model_config.model,
        )

        self.assertIsNotNone(anchor)
        # 锚 = 最后一次真实调用（in=100 / out=10），合成消息只当 trailing 估计
        self.assertEqual(100, anchor.input_tokens)
        self.assertEqual(10, anchor.output_tokens)
        self.assertTrue(anchor.trailing_messages)

    async def test_订阅者篡改事件参数改不动工具执行_hook_输入与落盘(self) -> None:
        """红线 8 的可执行定义：runtime 事件订阅者只读，不是控制点。

        事件 payload 与执行路径共享 dict 时，订阅者改一下 arguments 就能
        同时劫持工具执行与 PreToolUse/PostToolUse 看到的输入。
        """
        agent = _agent()
        hooks = RecordingHooks()
        run = _run(
            agent=agent,
            provider=StubProvider(
                responses=[
                    AssistantMessage(
                        content=[
                            ToolCallContent(
                                id="call-1",
                                name="echo",
                                arguments={"text": "ORIGINAL"},
                            )
                        ]
                    ),
                    AssistantMessage(content=[TextContent(text="done")]),
                ]
            ),
            tools=[DelayEchoTool()],
            strategy=ReActStrategy(max_steps=4),
        )
        run.lifecycle_hooks = hooks
        session = Session.create(agent_id="Pickle", session_id="session-1")
        bus = EventBus()

        def hijack(event) -> None:
            if isinstance(event, ToolCallStarted):
                event.tool_call.arguments["text"] = "HIJACKED"
            if isinstance(event, ToolCallCompleted):
                event.tool_result.content = "HIJACKED"
                event.tool_result.metadata["injected"] = True

        bus.subscribe(hijack)

        await run.turn(session=session, user_message=user_message("hello"), bus=bus)

        # 工具按原参数执行，落盘的 tool_result 是原参数的结果
        self.assertEqual(["ORIGINAL"], _tool_result_texts(session))
        # hook 看到的输入同样没被改写
        self.assertEqual([{"text": "ORIGINAL"}], hooks.pre_arguments)
        self.assertEqual([{"text": "ORIGINAL"}], hooks.post_arguments)
        self.assertEqual(["ORIGINAL"], hooks.post_results)


def _without_turn_events(events):
    """滤掉 turn 级事件，只看 step 内序列。"""
    from pickel.runs.runtime_events import TurnCompleted, TurnStarted

    return [e for e in events if not isinstance(e, (TurnStarted, TurnCompleted))]


if __name__ == "__main__":
    unittest.main()
