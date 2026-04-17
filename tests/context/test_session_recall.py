import unittest

from myopenclaw.context import (
    SessionRecallResult,
    SessionRecallSnippet,
    build_session_recall_message,
    render_session_recall,
)
from myopenclaw.context.service import ConversationContextService
from myopenclaw.conversations.session import Session


class SessionRecallTests(unittest.TestCase):
    def test_render_session_recall_uses_clean_context_without_metadata(self) -> None:
        result = SessionRecallResult(
            snippets=[
                SessionRecallSnippet(
                    text="User: 你好\nAssistant: 你好呀",
                    source_uri="viking://session/u/s/messages.jsonl",
                    score=0.8,
                )
            ]
        )

        rendered = render_session_recall(result)

        self.assertIsNotNone(rendered)
        assert rendered is not None
        self.assertIn("<Session_Retrieved_Context>", rendered)
        self.assertIn("User: 你好", rendered)
        self.assertNotIn("viking://", rendered)
        self.assertNotIn("score", rendered)

    def test_build_session_recall_message_uses_user_role(self) -> None:
        message = build_session_recall_message(
            SessionRecallResult(snippets=[SessionRecallSnippet(text="remember this")])
        )

        self.assertIsNotNone(message)
        assert message is not None
        self.assertEqual("user", message.role.value)
        self.assertIn("remember this", message.content)

    def test_render_session_recall_returns_none_for_empty_result(self) -> None:
        self.assertIsNone(render_session_recall(SessionRecallResult()))

    def test_build_prompt_messages_prepends_session_recall_without_session_mutation(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.append_user_message("current request")
        recall_message = build_session_recall_message(
            SessionRecallResult(snippets=[SessionRecallSnippet(text="prior context")])
        )

        messages = ConversationContextService().build_prompt_messages_from_session(
            session,
            session_recall_message=recall_message,
        )

        self.assertEqual(1, len(session.messages))
        self.assertEqual(2, len(messages))
        self.assertIn("<Session_Retrieved_Context>", messages[0].content)
        self.assertEqual("current request", messages[1].content)

    def test_render_session_recall_trims_snippets_over_budget(self) -> None:
        result = SessionRecallResult(
            snippets=[
                SessionRecallSnippet(text="first " * 20),
                SessionRecallSnippet(text="second " * 100),
            ]
        )

        rendered = render_session_recall(result, max_chars=500)

        self.assertIsNotNone(rendered)
        assert rendered is not None
        self.assertIn("first", rendered)
        self.assertNotIn("second", rendered)


if __name__ == "__main__":
    unittest.main()
