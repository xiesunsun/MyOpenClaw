from __future__ import annotations

import unittest

from pickel.conversations.agent_message import UserMessage
from pickel.conversations.content_blocks import TextContent
from pickel.runs.turn_mailbox import (
    PendingInputConflictError,
    TurnMailbox,
    TurnMailboxClosedError,
)


def _message(text: str) -> UserMessage:
    return UserMessage(content=[TextContent(text=text)])


class TurnMailboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_cancel_and_fifo(self) -> None:
        mailbox = TurnMailbox("turn-1")
        first = await mailbox.add(_message("one"))
        second = await mailbox.add(_message("two"))

        updated = await mailbox.update(
            second.input_id,
            _message("updated"),
            expected_revision=1,
        )

        self.assertEqual(2, updated.revision)
        self.assertEqual(first, await mailbox.take_steering())
        self.assertEqual(
            updated, await mailbox.cancel(updated.input_id, expected_revision=2)
        )
        self.assertIsNone(await mailbox.take_steering())

    async def test_stale_revision_is_rejected(self) -> None:
        mailbox = TurnMailbox("turn-1")
        item = await mailbox.add(_message("one"))
        await mailbox.update(item.input_id, _message("two"), expected_revision=1)

        with self.assertRaises(PendingInputConflictError):
            await mailbox.cancel(item.input_id, expected_revision=1)

    async def test_finish_closes_mailbox_atomically(self) -> None:
        mailbox = TurnMailbox("turn-1")

        self.assertIsNone(await mailbox.finish_or_take_steering())
        with self.assertRaises(TurnMailboxClosedError):
            await mailbox.add(_message("late"))


if __name__ == "__main__":
    unittest.main()
