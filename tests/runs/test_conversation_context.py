"""ConversationContextService 迁移窗口测试。

Session 已无线性 messages；本文件只验证 deprecated 服务在
UserTurn / SessionMessage 上的旧窗口行为。新组装路径见 tests/context。
"""

import unittest
import warnings

from pickel.context import ConversationContextService, UserTurn
from pickel.conversations.message import (
    MessageRole,
    SessionMessage,
    ToolCall,
    ToolCallBatch,
    ToolCallResult,
)


def _user(content: str) -> SessionMessage:
    return SessionMessage(role=MessageRole.USER, content=content)


def _assistant(content: str, batch: ToolCallBatch | None = None) -> SessionMessage:
    return SessionMessage(
        role=MessageRole.ASSISTANT,
        content=content,
        tool_call_batch=batch,
    )


class ConversationContextTests(unittest.TestCase):
    def test_collect_recent_user_turns_keeps_recent_turns_in_order(self) -> None:
        # 无 session.messages 时 deprecated 服务返回空；用 from_turns 验证窗口语义
        turns = [
            UserTurn(
                user_message=_user(f"user-{index}"),
                assistant_messages=[_assistant(f"answer-{index}")],
            )
            for index in range(6)
        ]
        # 模拟保留最近 3 轮（与 cli_turn_window=3 一致）
        kept = turns[-3:]
        messages = ConversationContextService.build_prompt_messages_from_turns(kept)

        self.assertEqual(6, len(messages))
        self.assertEqual(
            ["user-3", "answer-3", "user-4", "answer-4", "user-5", "answer-5"],
            [message.content for message in messages],
        )

    def test_collect_recent_user_turns_keeps_raw_tool_batches(self) -> None:
        batch1 = ToolCallBatch(
            batch_id="batch-1",
            step_index=1,
            calls=[
                ToolCall(
                    id="call-1",
                    name="read_file",
                    arguments={"path": "/tmp/first.py"},
                )
            ],
            results=[ToolCallResult(call_id="call-1", content="first result")],
        )
        batch2 = ToolCallBatch(
            batch_id="batch-2",
            step_index=1,
            calls=[
                ToolCall(
                    id="call-2",
                    name="read_file",
                    arguments={"path": "/tmp/second.py"},
                )
            ],
            results=[ToolCallResult(call_id="call-2", content="second result")],
        )
        turns = [
            UserTurn(
                user_message=_user("first user"),
                assistant_messages=[
                    _assistant("checking first file", batch1),
                    _assistant("first final"),
                ],
            ),
            UserTurn(
                user_message=_user("second user"),
                assistant_messages=[_assistant("checking second file", batch2)],
            ),
        ]

        messages = ConversationContextService.build_prompt_messages_from_turns(turns)

        self.assertEqual("first user", messages[0].content)
        self.assertIsNotNone(messages[1].tool_call_batch)
        self.assertEqual("first final", messages[2].content)
        self.assertEqual("second user", messages[3].content)
        self.assertIsNotNone(messages[4].tool_call_batch)

    def test_build_prompt_messages_from_turns_flattens_recent_turns(self) -> None:
        batch1 = ToolCallBatch(
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
        )
        batch2 = ToolCallBatch(
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
        )
        turns = [
            UserTurn(
                user_message=_user("completed user"),
                assistant_messages=[
                    _assistant("checked code", batch1),
                    _assistant("completed final"),
                ],
            ),
            UserTurn(
                user_message=_user("current user"),
                assistant_messages=[_assistant("checking current file", batch2)],
            ),
        ]

        messages = ConversationContextService.build_prompt_messages_from_turns(turns)

        self.assertEqual(
            [
                "completed user",
                "checked code",
                "completed final",
                "current user",
                "checking current file",
            ],
            [message.content for message in messages],
        )
        self.assertIsNotNone(messages[1].tool_call_batch)
        self.assertIsNotNone(messages[-1].tool_call_batch)

    def test_user_turn_rejects_non_user_first_message(self) -> None:
        with self.assertRaisesRegex(ValueError, "role 'user'"):
            UserTurn(user_message=_assistant("assistant only"))

    def test_collect_recent_user_turns_empty_without_linear_messages(self) -> None:
        from pickel.conversations.session import Session

        session = Session.create(agent_id="Pickle", session_id="session-1")
        service = ConversationContextService(cli_turn_window=3)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            turns = service.collect_recent_user_turns(session)
        self.assertEqual([], turns)
        self.assertTrue(any(issubclass(w.category, DeprecationWarning) for w in caught))


if __name__ == "__main__":
    unittest.main()
