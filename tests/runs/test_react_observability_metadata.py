"""ReAct 写入可观测性 metadata：context_fingerprint 与 hook_injected_chars。

context_fingerprint 是 UsageAnchor 的前提——不写入则锚永远失效，
每次 /context 都会退回远程 count（设计 §6.1）。
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from pickel.agents.agent import Agent
from pickel.context.assembler import ContextAssembler
from pickel.context.model_context import ModelContext, SystemContent
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.session import Session
from pickel.hooks.decisions import BeforeRequestDecision
from pickel.hooks.lifecycle import NoopLifecycleHooks
from pickel.providers.base import Provider
from pickel.tools.bus import ToolActivation, bus_with
from pickel.runs.run import Run
from pickel.runs.strategy.react import ReActStrategy
from pickel.runs.usage_anchor import context_fingerprint, resolve_anchor
from pickel.shared.model_config import ModelConfig
from pickel.tools.shell import LocalBashOperations


class RecordingProvider(Provider):
    def __init__(self, reply: AssistantMessage) -> None:
        self.reply = reply
        self.contexts: list[ModelContext] = []

    @classmethod
    def from_config(cls, config: ModelConfig) -> "RecordingProvider":
        raise NotImplementedError

    async def generate(self, context: ModelContext) -> AssistantMessage:
        self.contexts.append(context)
        return self.reply


class InjectingHooks(NoopLifecycleHooks):
    """before_request 往 system 里塞一段文本。"""

    INJECTED = "X" * 120

    def __init__(self) -> None:
        super().__init__()
        self.original: ModelContext | None = None

    async def before_request(self, event) -> BeforeRequestDecision:
        original = event.model_context
        self.original = original
        return BeforeRequestDecision(
            model_context=ModelContext(
                system=SystemContent.from_text(
                    original.system.as_text() + self.INJECTED
                ),
                messages=list(original.messages),
                tools=list(original.tools),
            )
        )


def _agent() -> Agent:
    return Agent(
        agent_id="Pickle",
        workspace_path=Path("."),
        behavior_path=Path("."),
        behavior_instruction="you are pickle",
        model_config=ModelConfig(provider="fake", model="fake-1"),
        tool_ids=[],
    )


def _run(provider, hooks=None) -> Run:
    return Run(
        agent=_agent(),
        provider=provider,  # type: ignore[arg-type]
        tool_bus=(_bus := bus_with([])),
        activation=ToolActivation(allowed=frozenset(_bus.list_names())),
        context_assembler=ContextAssembler(),
        lifecycle_hooks=hooks or NoopLifecycleHooks(),
        session_service=None,
        file_access_policy=None,
        workspace_files=None,
        bash_operations=LocalBashOperations(),
        unit_window=5,
        strategy=ReActStrategy(max_steps=2),
    )


def _reply() -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text="done")],
        metadata=ModelResponseMetadata(
            provider="fake",
            model="fake-1",
            usage=ModelUsage(input_tokens=100, output_tokens=10),
        ),
    )


class ReactObservabilityMetadataTests(unittest.TestCase):
    def test_writes_context_fingerprint_matching_sent_request(self) -> None:
        session = Session.create(agent_id="Pickle")
        session.append_user(UserMessage(content=[TextContent(text="hi")]))
        provider = RecordingProvider(_reply())
        run = _run(provider)

        asyncio.run(ReActStrategy(max_steps=2).execute(run=run, session=session))

        assistant = session.active_path()[-1]
        written = assistant.payload["metadata"]["context_fingerprint"]
        expected = context_fingerprint(
            provider.contexts[0], provider="fake", model="fake-1"
        )
        self.assertEqual(expected, written)

    def test_anchor_resolves_after_a_real_turn(self) -> None:
        """端到端：跑完一轮后 /context 能命中锚，不需要远程 count。"""
        session = Session.create(agent_id="Pickle")
        session.append_user(UserMessage(content=[TextContent(text="hi")]))
        provider = RecordingProvider(_reply())
        run = _run(provider)

        asyncio.run(ReActStrategy(max_steps=2).execute(run=run, session=session))

        anchor = resolve_anchor(
            session=session,
            request=provider.contexts[0],
            provider="fake",
            model="fake-1",
        )
        self.assertIsNotNone(anchor)
        self.assertEqual(100, anchor.input_tokens)
        self.assertEqual(10, anchor.output_tokens)

    def test_hook_injected_chars_is_zero_without_hooks(self) -> None:
        session = Session.create(agent_id="Pickle")
        session.append_user(UserMessage(content=[TextContent(text="hi")]))
        run = _run(RecordingProvider(_reply()))

        asyncio.run(ReActStrategy(max_steps=2).execute(run=run, session=session))

        assistant = session.active_path()[-1]
        self.assertEqual(0, assistant.payload["metadata"]["hook_injected_chars"])

    def test_hook_injected_chars_records_before_request_rewrite(self) -> None:
        session = Session.create(agent_id="Pickle")
        session.append_user(UserMessage(content=[TextContent(text="hi")]))
        run = _run(RecordingProvider(_reply()), hooks=InjectingHooks())

        asyncio.run(ReActStrategy(max_steps=2).execute(run=run, session=session))

        assistant = session.active_path()[-1]
        self.assertEqual(
            len(InjectingHooks.INJECTED),
            assistant.payload["metadata"]["hook_injected_chars"],
        )

    def test_fingerprint_reflects_prepare_output_not_post_hook_request(self) -> None:
        """指纹对应 prepare 输出（hook 前）。

        `/context` 预览不跑 hook，若指纹记 hook 后的 Request，有 hook 时锚会永远失效。
        usage 仍是 hook 后的真实值——锚因此已包含 hook 注入量，
        两者的差额由 hook_injected_chars 解释。
        """
        session = Session.create(agent_id="Pickle")
        session.append_user(UserMessage(content=[TextContent(text="hi")]))
        provider = RecordingProvider(_reply())
        hooks = InjectingHooks()
        run = _run(provider, hooks=hooks)

        asyncio.run(ReActStrategy(max_steps=2).execute(run=run, session=session))

        sent = provider.contexts[0]
        self.assertIn(InjectingHooks.INJECTED, sent.system.as_text())
        assistant = session.active_path()[-1]
        written = assistant.payload["metadata"]["context_fingerprint"]

        self.assertEqual(
            context_fingerprint(hooks.original, provider="fake", model="fake-1"),
            written,
        )
        self.assertNotEqual(
            context_fingerprint(sent, provider="fake", model="fake-1"),
            written,
        )


if __name__ == "__main__":
    unittest.main()
