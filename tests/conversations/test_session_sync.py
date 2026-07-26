import unittest

from pickel.conversations.session import Session
from pickel.conversations.session_sync import CompositeSessionSync, NoopSessionSync


class _RecordingSync:
    def __init__(self, name: str, *, boom: bool = False) -> None:
        self.name = name
        self.boom = boom
        self.calls: list[str] = []

    def sync_pending_messages(self, *, session: Session) -> None:
        self.calls.append("sync")
        if self.boom:
            raise RuntimeError(f"{self.name} exploded")

    def commit_pending_messages(self, *, session: Session, force: bool = False) -> None:
        self.calls.append(f"commit:{force}")
        if self.boom:
            raise RuntimeError(f"{self.name} exploded")

    def delete_session(self, *, session: Session) -> None:
        self.calls.append("delete")
        if self.boom:
            raise RuntimeError(f"{self.name} exploded")


class CompositeSessionSyncTests(unittest.TestCase):
    def test_calls_every_sync_in_order(self) -> None:
        first = _RecordingSync("first")
        second = _RecordingSync("second")
        composite = CompositeSessionSync([first, second])
        session = Session.create(agent_id="Pickle")

        composite.sync_pending_messages(session=session)
        composite.commit_pending_messages(session=session, force=True)
        composite.delete_session(session=session)

        self.assertEqual(["sync", "commit:True", "delete"], first.calls)
        self.assertEqual(["sync", "commit:True", "delete"], second.calls)

    def test_one_failing_sync_does_not_stop_the_others(self) -> None:
        boom = _RecordingSync("boom", boom=True)
        healthy = _RecordingSync("healthy")
        composite = CompositeSessionSync([boom, healthy])
        session = Session.create(agent_id="Pickle")

        composite.sync_pending_messages(session=session)

        self.assertEqual(["sync"], healthy.calls)

    def test_empty_composite_is_equivalent_to_noop(self) -> None:
        composite = CompositeSessionSync([])
        session = Session.create(agent_id="Pickle")

        composite.sync_pending_messages(session=session)
        composite.commit_pending_messages(session=session)
        composite.delete_session(session=session)

    def test_noop_accepts_every_call(self) -> None:
        noop = NoopSessionSync()
        session = Session.create(agent_id="Pickle")

        noop.sync_pending_messages(session=session)
        noop.commit_pending_messages(session=session, force=True)
        noop.delete_session(session=session)


if __name__ == "__main__":
    unittest.main()
