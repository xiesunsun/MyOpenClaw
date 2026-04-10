import unittest
from collections import deque

from myopenclaw.conversations.message import MessageRole, ToolCall, ToolCallBatch, ToolCallResult
from myopenclaw.conversations.session import Session
from myopenclaw.context import (
    ConversationContextBuilder,
    ConversationWindow,
    ConversationWindowManager,
)


class ConversationContextTests(unittest.TestCase):
    def test_sync_with_session_builds_completed_and_current_turns_incrementally(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.append_user_message("first user")
        session.append_assistant_tool_batch(
            ToolCallBatch(
                batch_id="batch-1",
                step_index=1,
                calls=[
                    ToolCall(
                        id="call-1",
                        name="read_file",
                        arguments={"path": "/tmp/first.py"},
                    )
                ],
                results=[
                    ToolCallResult(
                        call_id="call-1",
                        content="first result",
                    )
                ],
            ),
            content="checking first file",
        )
        session.append_assistant_message("first final")
        session.append_user_message("second user")
        session.append_assistant_tool_batch(
            ToolCallBatch(
                batch_id="batch-2",
                step_index=1,
                calls=[
                    ToolCall(
                        id="call-2",
                        name="read_file",
                        arguments={"path": "/tmp/second.py"},
                    )
                ],
                results=[
                    ToolCallResult(
                        call_id="call-2",
                        content="second result",
                    )
                ],
            ),
            content="checking second file",
        )

        window = ConversationWindow()
        manager = ConversationWindowManager(cli_turn_window=5)

        manager.sync_with_session(session=session, window=window)

        self.assertEqual(4, window.last_consumed_message_index)
        self.assertEqual(1, len(window.completed_turns))
        self.assertEqual("first user", window.completed_turns[0].user_message.content)
        self.assertEqual("first final", window.completed_turns[0].final_answer.content)
        self.assertEqual(1, len(window.completed_turns[0].tool_steps))
        self.assertIsNotNone(window.current_turn)
        self.assertEqual("second user", window.current_turn.user_message.content)
        self.assertEqual(1, len(window.current_turn.tool_steps))
        self.assertIsNone(window.current_turn.final_answer)

    def test_build_messages_summarizes_completed_turns_and_keeps_current_turn_raw(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.append_user_message("completed user")
        session.append_assistant_tool_batch(
            ToolCallBatch(
                batch_id="batch-1",
                step_index=1,
                calls=[
                    ToolCall(
                        id="call-1",
                        name="grep",
                        arguments={"pattern": "ConversationWindow", "path": "/tmp/project/alpha.py"},
                    )
                ],
                results=[
                    ToolCallResult(
                        call_id="call-1",
                        content="matched line 1\nmatched line 2",
                    )
                ],
            ),
            content="checked code",
        )
        session.append_assistant_message("completed final")
        session.append_user_message("current user")
        session.append_assistant_tool_batch(
            ToolCallBatch(
                batch_id="batch-2",
                step_index=1,
                calls=[
                    ToolCall(
                        id="call-2",
                        name="read_file",
                        arguments={"path": "/tmp/project/current.py"},
                    )
                ],
                results=[
                    ToolCallResult(
                        call_id="call-2",
                        content="current raw result",
                    )
                ],
            ),
            content="checking current file",
        )

        window = ConversationWindow()
        manager = ConversationWindowManager(cli_turn_window=5)
        builder = ConversationContextBuilder()

        manager.sync_with_session(session=session, window=window)
        messages = builder.build_messages(window=window)

        self.assertEqual(
            ["completed user", "Tool step summary:\n- grep args={\"path\": \"/tmp/project/alpha.py\", \"pattern\": \"ConversationWindow\"} -> result=matched line 1 matched line 2", "completed final", "current user", "checking current file"],
            [message.content for message in messages],
        )
        self.assertIsNone(messages[1].tool_call_batch)
        self.assertIsNotNone(messages[-1].tool_call_batch)

    def test_sync_with_session_trims_completed_turns_to_cli_window(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1")
        for index in range(6):
            session.append_user_message(f"user-{index}")
            session.append_assistant_message(f"answer-{index}")

        window = ConversationWindow()
        manager = ConversationWindowManager(cli_turn_window=5)

        manager.sync_with_session(session=session, window=window)

        self.assertEqual(5, len(window.completed_turns))
        self.assertEqual(
            deque([1, 2, 3, 4, 5]),
            deque(turn.turn_index for turn in window.completed_turns),
        )
        self.assertEqual("user-1", window.completed_turns[0].user_message.content)
        self.assertEqual("answer-5", window.completed_turns[-1].final_answer.content)


if __name__ == "__main__":
    unittest.main()
