import unittest
from datetime import datetime, timedelta, timezone

from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextContent
from pickel.conversations.session import Session
from pickel.integrations.openviking.commit_policy import ThresholdCommitPolicy
from pickel.integrations.openviking.openviking_state import OpenVikingSessionState


class CommitPolicyTests(unittest.TestCase):
    def test_does_not_commit_without_pending_remote_commit(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1")
        state = OpenVikingSessionState()
        policy = ThresholdCommitPolicy(
            commit_after=timedelta(minutes=30),
            commit_after_turns=1,
        )

        self.assertFalse(
            policy.should_commit(
                session=session,
                state=state,
                now=datetime(2026, 4, 13, tzinfo=timezone.utc),
            )
        )

    def test_commits_when_time_threshold_is_reached(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.append_assistant(AssistantMessage(content=[TextContent(text="one")]))
        state = OpenVikingSessionState()
        state.mark_messages_synced(remote_session_id="session-1", last_message_index=0)
        state.mark_messages_committed(
            last_message_index=-1,
            committed_at=datetime(2026, 4, 13, tzinfo=timezone.utc),
        )
        policy = ThresholdCommitPolicy(
            commit_after=timedelta(minutes=30),
            commit_after_turns=8,
        )

        self.assertTrue(
            policy.should_commit(
                session=session,
                state=state,
                now=datetime(2026, 4, 13, 0, 30, tzinfo=timezone.utc),
            )
        )

    def test_commits_when_assistant_turn_threshold_is_reached(self) -> None:
        session = Session.create(agent_id="Pickle", session_id="session-1")
        session.append_user(UserMessage(content=[TextContent(text="hello")]))
        session.append_assistant(AssistantMessage(content=[TextContent(text="one")]))
        session.append_assistant(AssistantMessage(content=[TextContent(text="two")]))
        state = OpenVikingSessionState()
        state.mark_messages_synced(remote_session_id="session-1", last_message_index=2)
        policy = ThresholdCommitPolicy(
            commit_after=timedelta(minutes=30),
            commit_after_turns=2,
        )

        self.assertTrue(
            policy.should_commit(
                session=session,
                state=state,
                now=datetime(2026, 4, 13, tzinfo=timezone.utc),
            )
        )


if __name__ == "__main__":
    unittest.main()
