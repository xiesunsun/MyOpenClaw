import asyncio
import unittest
from dataclasses import dataclass

from pickel.runtime.host_calls import (
    HostCallCancelled,
    HostCallContext,
    HostCallDeadlineExceeded,
    HostCallFailed,
    HostCallHandlerAlreadyRegisteredError,
    HostCallRouter,
    HostCallSpec,
    HostCallUnavailable,
)
from pickel.shared.execution_identity import ExecutionIdentity


@dataclass(frozen=True)
class _Request:
    value: str


@dataclass(frozen=True)
class _Response:
    value: str


SPEC = HostCallSpec(
    name="test.echo",
    version=1,
    request_type=_Request,
    response_type=_Response,
)


class HostCallRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_distinct_host_calls_share_tool_identity_but_have_unique_call_ids(
        self,
    ) -> None:
        router = HostCallRouter()
        seen = []
        identity = ExecutionIdentity(
            session_id="session-1",
            operation_id="operation-1",
            step_id="step-1",
            tool_call_id="tool-call-1",
        )

        async def handler(_request, context):
            seen.append(context)
            return _Response("ok")

        router.register(SPEC, handler)
        await router.client.call(
            SPEC, _Request("first"), HostCallContext(identity=identity)
        )
        await router.client.call(
            SPEC, _Request("second"), HostCallContext(identity=identity)
        )

        self.assertEqual(2, len(seen))
        self.assertNotEqual(seen[0].call_id, seen[1].call_id)
        self.assertIs(seen[0].identity, identity)
        self.assertIs(seen[1].identity, identity)

    async def test_missing_handler_is_explicit(self) -> None:
        outcome = await HostCallRouter().client.call(
            SPEC,
            _Request("x"),
            HostCallContext(),
        )
        self.assertEqual(HostCallUnavailable("no_handler"), outcome)

    async def test_duplicate_registration_is_rejected(self) -> None:
        router = HostCallRouter()
        router.register(SPEC, lambda _request, _ctx: _Response("one"))
        with self.assertRaises(HostCallHandlerAlreadyRegisteredError):
            router.register(SPEC, lambda _request, _ctx: _Response("two"))

    async def test_invalid_response_is_failed_outcome(self) -> None:
        router = HostCallRouter()
        router.register(SPEC, lambda _request, _ctx: "wrong")  # type: ignore[arg-type]
        outcome = await router.client.call(SPEC, _Request("x"), HostCallContext())
        self.assertIsInstance(outcome, HostCallFailed)
        self.assertEqual("InvalidResponse", outcome.error_type)

    async def test_timeout_cancels_handler(self) -> None:
        cancelled = asyncio.Event()

        async def handler(_request, _ctx):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        router = HostCallRouter()
        router.register(SPEC, handler)
        outcome = await router.client.call(
            SPEC,
            _Request("x"),
            HostCallContext(timeout_seconds=0.01),
        )
        self.assertIsInstance(outcome, HostCallDeadlineExceeded)
        self.assertTrue(cancelled.is_set())

    async def test_closing_lease_cancels_active_call(self) -> None:
        started = asyncio.Event()

        async def handler(_request, _ctx):
            started.set()
            await asyncio.Future()

        router = HostCallRouter()
        lease = router.register(SPEC, handler)
        task = asyncio.create_task(
            router.client.call(SPEC, _Request("x"), HostCallContext())
        )
        await started.wait()
        lease.close()

        outcome = await task
        self.assertEqual(HostCallCancelled("provider_detached"), outcome)
        self.assertFalse(router.supports(SPEC))

    async def test_close_cancels_active_and_rejects_new_calls(self) -> None:
        started = asyncio.Event()

        async def handler(_request, _ctx):
            started.set()
            await asyncio.Future()

        router = HostCallRouter()
        router.register(SPEC, handler)
        task = asyncio.create_task(
            router.client.call(SPEC, _Request("x"), HostCallContext())
        )
        await started.wait()
        await router.close()

        self.assertEqual(HostCallCancelled("router_closed"), await task)
        self.assertEqual(
            HostCallUnavailable("router_closed"),
            await router.client.call(SPEC, _Request("y"), HostCallContext()),
        )

    async def test_caller_cancellation_propagates(self) -> None:
        started = asyncio.Event()

        async def handler(_request, _ctx):
            started.set()
            await asyncio.Future()

        router = HostCallRouter()
        router.register(SPEC, handler)
        task = asyncio.create_task(
            router.client.call(SPEC, _Request("x"), HostCallContext())
        )
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
