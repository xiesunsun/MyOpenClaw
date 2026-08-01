import unittest

from pickel.cli.host_call_handlers import CliHostCallHandlers
from pickel.runs.host_call_types import (
    CONFIRMATION_CALL,
    STRUCTURED_INPUT_CALL,
    ConfirmationRequest,
    HostCallSource,
    StructuredInputRequest,
)
from pickel.runs.host_calls import HostCallCompleted, HostCallContext
from pickel.runs.runtime_bus import RuntimeBus


class CliHostCallHandlersTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmation_and_structured_input(self) -> None:
        answers = iter(["y", "Ada", "2", "yes"])
        rendered = []

        async def read(_prompt: str) -> str:
            return next(answers)

        bus = RuntimeBus()
        handlers = CliHostCallHandlers(
            input_reader=read,
            render_message=rendered.append,
        )
        handlers.attach(bus)
        source = HostCallSource(kind="mcp", name="fixture")

        confirmation = await bus.host_calls.client.call(
            CONFIRMATION_CALL,
            ConfirmationRequest(source=source, title="Confirm", message="Continue?"),
            HostCallContext(),
        )
        structured = await bus.host_calls.client.call(
            STRUCTURED_INPUT_CALL,
            StructuredInputRequest(
                source=source,
                title="Details",
                message="Enter values",
                schema={
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "count": {"type": "integer"},
                        "enabled": {"type": "boolean"},
                    },
                    "required": ["name", "count", "enabled"],
                },
            ),
            HostCallContext(),
        )

        self.assertEqual("accept", confirmation.value.decision)
        self.assertIsInstance(structured, HostCallCompleted)
        self.assertEqual(
            {"name": "Ada", "count": 2, "enabled": True},
            structured.value.content,
        )
        self.assertIn("Confirm\nContinue?", rendered)
        self.assertIn("Details\nEnter values", rendered)

    async def test_invalid_field_is_asked_again(self) -> None:
        answers = iter(["not-an-int", "3"])
        rendered = []

        async def read(_prompt: str) -> str:
            return next(answers)

        bus = RuntimeBus()
        CliHostCallHandlers(
            input_reader=read,
            render_message=rendered.append,
        ).attach(bus)
        outcome = await bus.host_calls.client.call(
            STRUCTURED_INPUT_CALL,
            StructuredInputRequest(
                source=HostCallSource(kind="mcp", name="fixture"),
                title="Count",
                message="Enter count",
                schema={
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                },
            ),
            HostCallContext(),
        )

        self.assertEqual({"count": 3}, outcome.value.content)
        self.assertIn("请输入整数", rendered)
