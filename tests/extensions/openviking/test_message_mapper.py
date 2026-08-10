import unittest

from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextContent, ToolCallContent
from pickel.extensions.openviking.message_mapper import OpenVikingMessageMapper


class OpenVikingMessageMapperTests(unittest.TestCase):
    def test_maps_plain_user_message_to_text_part(self) -> None:
        mapper = OpenVikingMessageMapper()
        message = UserMessage(content=[TextContent(text="hello")])

        payload = mapper.to_openviking_message(message)

        self.assertEqual("user", payload.role)
        self.assertEqual("hello", payload.content)
        self.assertEqual([{"type": "text", "text": "hello"}], payload.parts)

    def test_maps_plain_assistant_message_to_text_part(self) -> None:
        mapper = OpenVikingMessageMapper()
        message = AssistantMessage(content=[TextContent(text="hi")])

        payload = mapper.to_openviking_message(message)

        self.assertEqual("assistant", payload.role)
        self.assertEqual("hi", payload.content)
        self.assertEqual([{"type": "text", "text": "hi"}], payload.parts)

    def test_maps_assistant_tool_call_to_tool_part(self) -> None:
        mapper = OpenVikingMessageMapper()
        message = AssistantMessage(
            content=[
                TextContent(text="checking"),
                ToolCallContent(
                    id="call-1",
                    name="read_file",
                    arguments={"path": "README.md"},
                ),
            ],
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
                    "tool_output": "",
                    "tool_status": "completed",
                },
            ],
            payload.parts,
        )

    def test_truncates_tool_output_and_marks_errors(self) -> None:
        mapper = OpenVikingMessageMapper(tool_output_max_chars=4)
        message = ToolResultMessage(
            tool_call_id="call-1",
            tool_name="shell",
            content=[TextContent(text="abcdef")],
            is_error=True,
        )

        payload = mapper.to_openviking_message(message)

        self.assertEqual("assistant", payload.role)
        self.assertIsNone(payload.content)
        self.assertEqual("abcd", payload.parts[0]["tool_output"])
        self.assertEqual("error", payload.parts[0]["tool_status"])


if __name__ == "__main__":
    unittest.main()
