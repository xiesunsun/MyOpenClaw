import unittest

from myopenclaw.domain.message import (
    MessageRole,
    SessionMessage,
    ToolCall,
    ToolCallBatch,
    ToolCallResult,
)
from myopenclaw.domain.metadata import MessageMetadata
from myopenclaw.domain.session import Session


class SessionTests(unittest.TestCase):
    def test_session_create_binds_session_to_agent(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1")

        self.assertEqual("session-1", session.session_id)
        self.assertEqual("Pickle", session.agent_id)
        self.assertEqual([], session.messages)

    def test_session_belongs_to_one_agent_and_stores_model_visible_messages(self) -> None:
        session = Session(session_id="session-1", agent_id="Pickle")

        session.append_user_message("hello")
        metadata = MessageMetadata(
            provider="google/gemini",
            model="gemini-3-flash-preview",
            input_tokens=12,
            output_tokens=8,
            elapsed_ms=34,
        )
        session.append_assistant_message("hi there", metadata=metadata)

        self.assertEqual("Pickle", session.agent_id)
        self.assertEqual(
            [
                SessionMessage(role=MessageRole.USER, content="hello"),
                SessionMessage(
                    role=MessageRole.ASSISTANT,
                    content="hi there",
                    metadata=metadata,
                ),
            ],
            session.messages,
        )

    def test_session_can_store_assistant_tool_batch(self) -> None:
        session = Session(session_id="session-1", agent_id="Pickle")

        batch = ToolCallBatch(
            batch_id="batch-1",
            step_index=1,
            calls=[
                ToolCall(
                    id="call-1",
                    name="echo",
                    arguments={"text": "ping"},
                )
            ],
            results=[
                ToolCallResult(
                    call_id="call-1",
                    content="ping",
                    metadata={"exit_code": 0},
                )
            ],
        )
        session.append_assistant_tool_batch(batch)

        self.assertEqual(MessageRole.ASSISTANT, session.messages[0].role)
        self.assertEqual("echo", session.messages[0].tool_call_batch.calls[0].name)
        self.assertEqual("call-1", session.messages[0].tool_call_batch.results[0].call_id)
        self.assertEqual({"exit_code": 0}, session.messages[0].tool_call_batch.results[0].metadata)


if __name__ == "__main__":
    unittest.main()
