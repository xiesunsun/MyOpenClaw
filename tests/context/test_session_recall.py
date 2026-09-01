import unittest

from pickel.context import (
    SessionRecallResult,
    SessionRecallSnippet,
    render_session_recall,
)


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

    def test_render_session_recall_returns_none_for_empty_result(self) -> None:
        self.assertIsNone(render_session_recall(SessionRecallResult()))

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
