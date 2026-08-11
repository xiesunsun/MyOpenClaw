import asyncio
import unittest

import mcp.types

from pickel.extensions.mcp.elicitation_mapper import resolve_elicitation
from pickel.runtime.host_call_types import (
    STRUCTURED_INPUT_CALL,
    StructuredInputAnswer,
)
from pickel.runtime.host_calls import HostCallContext, HostCallRouter


def _params() -> mcp.types.ElicitRequestFormParams:
    return mcp.types.ElicitRequestFormParams(
        message="Enter count",
        requested_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
        },
    )


class ElicitationMapperTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_schema_valid_content(self) -> None:
        router = HostCallRouter()
        router.register(
            STRUCTURED_INPUT_CALL,
            lambda _request, _context: StructuredInputAnswer(
                action="accept", content={"count": 2}
            ),
        )

        result = await resolve_elicitation(
            _params(),
            host_calls=router.client,
            context=HostCallContext(),
            server_name="fixture",
            tool_name="tool",
        )

        self.assertEqual("accept", result.action)
        self.assertEqual({"count": 2}, result.content)

    async def test_invalid_content_is_cancelled(self) -> None:
        router = HostCallRouter()
        router.register(
            STRUCTURED_INPUT_CALL,
            lambda _request, _context: StructuredInputAnswer(
                action="accept", content={"count": "wrong"}
            ),
        )

        result = await resolve_elicitation(
            _params(),
            host_calls=router.client,
            context=HostCallContext(),
            server_name="fixture",
            tool_name="tool",
        )

        self.assertEqual("cancel", result.action)

    async def test_no_handler_and_timeout_are_cancelled(self) -> None:
        no_handler = await resolve_elicitation(
            _params(),
            host_calls=HostCallRouter().client,
            context=HostCallContext(),
            server_name="fixture",
            tool_name="tool",
        )

        router = HostCallRouter()

        async def never_returns(_request, _context):
            await asyncio.Future()

        router.register(STRUCTURED_INPUT_CALL, never_returns)
        timeout = await resolve_elicitation(
            _params(),
            host_calls=router.client,
            context=HostCallContext(timeout_seconds=0.01),
            server_name="fixture",
            tool_name="tool",
        )

        self.assertEqual("cancel", no_handler.action)
        self.assertEqual("cancel", timeout.action)
