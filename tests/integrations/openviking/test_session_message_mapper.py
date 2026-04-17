import unittest

from myopenclaw.conversations.message import (
    MessageRole,
    SessionMessage,
    ToolCall,
    ToolCallBatch,
    ToolCallResult,
)
from myopenclaw.integrations.openviking.session_message_mapper import SessionMessageMapper


class SessionMessageMapperTests(unittest.TestCase):
    def test_maps_plain_user_message_to_text_part(self) -> None:
        mapper = SessionMessageMapper()
        message = SessionMessage(role=MessageRole.USER, content="hello")

        payload = mapper.to_openviking_message(message)

        self.assertEqual("user", payload.role)
        self.assertEqual("hello", payload.content)
        self.assertEqual([{"type": "text", "text": "hello"}], payload.parts)

    def test_maps_plain_assistant_message_to_text_part(self) -> None:
        mapper = SessionMessageMapper()
        message = SessionMessage(role=MessageRole.ASSISTANT, content="hi")

        payload = mapper.to_openviking_message(message)

        self.assertEqual("assistant", payload.role)
        self.assertEqual("hi", payload.content)
        self.assertEqual([{"type": "text", "text": "hi"}], payload.parts)

    def test_maps_assistant_tool_batch_to_tool_parts(self) -> None:
        mapper = SessionMessageMapper()
        message = SessionMessage(
            role=MessageRole.ASSISTANT,
            content="checking",
            tool_call_batch=ToolCallBatch(
                batch_id="batch-1",
                step_index=1,
                calls=[
                    ToolCall(
                        id="call-1",
                        name="read_file",
                        arguments={"path": "README.md"},
                    )
                ],
                results=[
                    ToolCallResult(
                        call_id="call-1",
                        content="contents",
                        metadata={"exit_code": 0},
                    )
                ],
            ),
        )

        payload = mapper.to_openviking_message(message)

        self.assertEqual("assistant", payload.role)
        self.assertEqual(
            [
                {"type": "text", "text": "checking"},
                {
                    "type": "tool",
                    "tool_id": "call-1",
                    "tool_name": "read_file",
                    "tool_input": {"path": "README.md"},
                    "tool_output": "contents",
                    "tool_status": "completed",
                },
            ],
            payload.parts,
        )

    def test_truncates_tool_output_and_marks_errors(self) -> None:
        mapper = SessionMessageMapper(tool_output_max_chars=4)
        message = SessionMessage(
            role=MessageRole.ASSISTANT,
            tool_call_batch=ToolCallBatch(
                batch_id="batch-1",
                step_index=1,
                calls=[ToolCall(id="call-1", name="shell", arguments={})],
                results=[
                    ToolCallResult(
                        call_id="call-1",
                        content="abcdef",
                        is_error=True,
                    )
                ],
            ),
        )

        payload = mapper.to_openviking_message(message)

        self.assertEqual("abcd", payload.parts[0]["tool_output"])
        self.assertEqual("error", payload.parts[0]["tool_status"])


if __name__ == "__main__":
    unittest.main()
