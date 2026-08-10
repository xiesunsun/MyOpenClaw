"""RequestDigest 事件:摘要不含正文,发射于 hook 之后 generate 之前。"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

from pickel.agents.agent import Agent
from pickel.runs.legacy_model_context_builder import LegacyModelContextBuilder
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.session import Session
from pickel.hooks.lifecycle import NoopLifecycleHooks
from pickel.providers.base import Provider
from pickel.runs.event_bus import EventBus
from pickel.runs.run import Run
from pickel.runs.strategy.react import ReActStrategy
from pickel.shared.model_config import ModelConfig
from pickel.tools.bus import ToolActivation, bus_with
from pickel.tools.shell import LocalBashOperations


class RecordingProvider(Provider):
    def __init__(self, reply: AssistantMessage) -> None:
        self.reply = reply
        self.contexts = []

    @classmethod
    def from_config(cls, config: ModelConfig) -> "RecordingProvider":
        raise NotImplementedError

    async def generate(self, context):
        self.contexts.append(context)
        return self.reply


def _run(provider) -> Run:
    return Run(
        agent=Agent(
            agent_id="Pickle",
            workspace_path=Path("."),
            behavior_path=Path("."),
            behavior_instruction="you are pickle SECRET-BEHAVIOR",
            model_config=ModelConfig(provider="fake", model="fake-1"),
            tool_ids=[],
        ),
        provider=provider,  # type: ignore[arg-type]
        tool_bus=(bus := bus_with([])),
        activation=ToolActivation(allowed=frozenset(bus.list_names())),
        model_context_builder=LegacyModelContextBuilder(),
        lifecycle_hooks=NoopLifecycleHooks(),
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


class RequestDigestEventTests(unittest.TestCase):
    def _execute(self) -> list:
        session = Session.create(agent_id="Pickle")
        session.append_user(UserMessage(content=[TextContent(text="hi SECRET-QUERY")]))
        bus = EventBus()
        events = []
        bus.subscribe(events.append)
        run = _run(RecordingProvider(_reply()))
        asyncio.run(
            ReActStrategy(max_steps=2).execute(run=run, session=session, bus=bus)
        )
        return events

    def test_digest_emitted_per_step_with_summary_fields(self) -> None:
        events = self._execute()

        digests = [e for e in events if e.EVENT_TYPE == "request_digest"]
        self.assertEqual(1, len(digests))
        digest = digests[0]
        payload = digest.to_dict()
        self.assertEqual(1, payload["step_index"])
        self.assertEqual(1, payload["message_count"])
        self.assertEqual([], payload["tool_names"])
        self.assertGreater(payload["request_chars"], 0)
        self.assertEqual(0, payload["hook_injected_chars"])
        section_names = [s["name"] for s in payload["system_sections"]]
        self.assertIn("behavior", section_names)
        for section in payload["system_sections"]:
            self.assertGreaterEqual(section["chars"], 1)

    def test_digest_contains_no_content_text(self) -> None:
        events = self._execute()

        digests = [e for e in events if e.EVENT_TYPE == "request_digest"]
        serialized = json.dumps(digests[0].to_dict(), ensure_ascii=False)
        self.assertNotIn("SECRET", serialized)

    def test_digest_ordered_after_step_started_before_assistant(self) -> None:
        events = self._execute()

        types = [e.EVENT_TYPE for e in events]
        self.assertLess(types.index("step_started"), types.index("request_digest"))
        self.assertLess(types.index("request_digest"), types.index("assistant_message"))


if __name__ == "__main__":
    unittest.main()
