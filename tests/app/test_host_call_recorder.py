import unittest

from pickel.app.host_call_recorder import SessionHostCallRecorder
from pickel.context.projection import project_messages
from pickel.conversations.session import Session
from pickel.runs.host_call_types import (
    STRUCTURED_INPUT_CALL,
    HostCallSource,
    StructuredInputAnswer,
    StructuredInputRequest,
)
from pickel.runs.host_calls import HostCallCompleted, HostCallContext, HostCallRouter


class _SessionService:
    def __init__(self) -> None:
        self.flushed = []

    def flush_new_entries(self, *, session, entries) -> None:
        self.flushed.extend(entries)


class SessionHostCallRecorderTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_request_and_response_without_model_projection(self) -> None:
        session = Session.create(agent_id="Pickle")
        service = _SessionService()
        router = HostCallRouter(
            SessionHostCallRecorder(session=session, session_service=service)
        )
        router.register(
            STRUCTURED_INPUT_CALL,
            lambda _request, _context: StructuredInputAnswer(
                action="accept",
                content={"name": "Ada"},
            ),
        )

        outcome = await router.client.call(
            STRUCTURED_INPUT_CALL,
            StructuredInputRequest(
                source=HostCallSource(kind="mcp", name="fixture"),
                title="Input",
                message="Name",
                schema={"type": "object"},
            ),
            HostCallContext(
                call_id="call-1",
                session_id=session.session_id,
                turn_id="turn-1",
                tool_call_id="tool-1",
            ),
        )

        self.assertEqual(
            HostCallCompleted(
                StructuredInputAnswer(action="accept", content={"name": "Ada"})
            ),
            outcome,
        )
        self.assertEqual(
            ["host_call_request", "host_call_response"],
            [entry.entry_type for entry in session.active_path()],
        )
        self.assertEqual(session.entries, service.flushed)
        self.assertEqual([], project_messages(session.active_path()))
