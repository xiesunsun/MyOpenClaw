"""会话输出总线。"""

import unittest

from pickel.runs.conversation_output_bus import ConversationOutputBus
from pickel.shared.conversation_output import AudioContent, AudioOutputReady


class ConversationOutputBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_publishes_to_subscribers_in_order(self) -> None:
        bus = ConversationOutputBus()
        calls = []
        bus.subscribe(lambda output: calls.append("first"))

        async def second(output) -> None:
            calls.append("second")

        bus.subscribe(second)

        await bus.publish(
            AudioOutputReady(
                session_id="session-1",
                turn_id="turn-1",
                source="test",
                audio=AudioContent(data=b"wav", media_type="audio/wav"),
            )
        )

        self.assertEqual(["first", "second"], calls)

    async def test_unsubscribe_stops_delivery(self) -> None:
        bus = ConversationOutputBus()
        outputs = []
        unsubscribe = bus.subscribe(outputs.append)
        unsubscribe()

        await bus.publish(
            AudioOutputReady(audio=AudioContent(data=b"wav", media_type="audio/wav"))
        )

        self.assertEqual([], outputs)
