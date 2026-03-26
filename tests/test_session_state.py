import unittest
from pathlib import Path
import sys

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from myopenclaw.conversation.message import MessageRole
from myopenclaw.conversation.state import SessionState


class SessionStateTests(unittest.TestCase):
    def test_new_state_starts_empty(self) -> None:
        state = SessionState(session_id="s1")

        self.assertEqual(state.messages, [])

    def test_add_user_and_assistant_messages_append_in_order(self) -> None:
        state = SessionState(session_id="s1")

        state.add_user_message("hello")
        state.add_assistant_message("hi")

        self.assertEqual(
            [(message.role, message.text) for message in state.messages],
            [
                (MessageRole.USER, "hello"),
                (MessageRole.ASSISTANT, "hi"),
            ],
        )
