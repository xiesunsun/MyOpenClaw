import unittest

from myopenclaw.context import ConversationContextService, UserTurn
from myopenclaw.conversations.message import ToolCall, ToolCallBatch, ToolCallResult
from myopenclaw.conversations.session import Session


class ConversationContextTests(unittest.TestCase):
    def test_collect_recent_user_turns_keeps_recent_turns_in_order(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1")
        for index in range(6):
            session.append_user_message(f"user-{index}")
            session.append_assistant_message(f"answer-{index}")

        service = ConversationContextService(cli_turn_window=3)

        turns = service.collect_recent_user_turns(session)

        self.assertEqual(3, len(turns))
        self.assertEqual(
            ["user-3", "user-4", "user-5"],
            [turn.user_message.content for turn in turns],
        )
        self.assertEqual(
            [["answer-3"], ["answer-4"], ["answer-5"]],
            [[message.content for message in turn.assistant_messages] for turn in turns],
        )

    def test_collect_recent_user_turns_keeps_raw_tool_batches(self) -> None:
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

        service = ConversationContextService(cli_turn_window=5)

        turns = service.collect_recent_user_turns(session)

        self.assertEqual(2, len(turns))
        self.assertEqual("first user", turns[0].user_message.content)
        self.assertEqual("first final", turns[0].assistant_messages[-1].content)
        self.assertIsNotNone(turns[0].assistant_messages[0].tool_call_batch)
        self.assertEqual("second user", turns[1].user_message.content)
        self.assertEqual(1, len(turns[1].assistant_messages))
        self.assertIsNotNone(turns[1].assistant_messages[0].tool_call_batch)

    def test_build_prompt_messages_from_session_flattens_recent_turns(self) -> None:
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
                        arguments={"pattern": "UserTurn", "path": "/tmp/project/alpha.py"},
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

        service = ConversationContextService(cli_turn_window=5)

        messages = service.build_prompt_messages_from_session(session)

        self.assertEqual(
            ["completed user", "checked code", "completed final", "current user", "checking current file"],
            [message.content for message in messages],
        )
        self.assertIsNotNone(messages[1].tool_call_batch)
        self.assertIsNotNone(messages[-1].tool_call_batch)

    def test_user_turn_rejects_non_user_first_message(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.append_assistant_message("assistant only")

        with self.assertRaisesRegex(ValueError, "role 'user'"):
            UserTurn(user_message=session.messages[0])


if __name__ == "__main__":
    unittest.main()
