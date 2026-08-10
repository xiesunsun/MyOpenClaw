"""OpenVikingRecall 适配器单测。"""

from __future__ import annotations

import unittest

from pickel.context.session_recall import SessionRecallResult, SessionRecallSnippet
from pickel.conversations.agent_message import UserMessage
from pickel.extensions.openviking.recall_adapter import OpenVikingRecall


class _StubProvider:
    def __init__(self, result: SessionRecallResult) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def recall(
        self,
        *,
        session_id: str,
        current_user_text: str,
    ) -> SessionRecallResult:
        self.calls.append(
            {"session_id": session_id, "current_user_text": current_user_text}
        )
        return self.result


class OpenVikingRecallAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_provide_returns_user_message_from_snippets(self) -> None:
        provider = _StubProvider(
            SessionRecallResult(snippets=[SessionRecallSnippet(text="prior context")])
        )
        adapter = OpenVikingRecall(provider, max_chars=6000)
        messages = await adapter.provide(
            session_id="session-1",
            current_user_text="hello",
        )

        self.assertEqual(1, len(messages))
        self.assertIsInstance(messages[0], UserMessage)
        self.assertIn("prior context", messages[0].content[0].text)
        self.assertIn("<Session_Retrieved_Context>", messages[0].content[0].text)
        self.assertEqual(
            [{"session_id": "session-1", "current_user_text": "hello"}],
            provider.calls,
        )

    async def test_provide_returns_empty_when_no_snippets(self) -> None:
        adapter = OpenVikingRecall(_StubProvider(SessionRecallResult()), max_chars=100)
        messages = await adapter.provide(
            session_id="session-1",
            current_user_text="x",
        )
        self.assertEqual([], messages)


if __name__ == "__main__":
    unittest.main()
