"""一次性、无人值守的 Unix Query Surface。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, TextIO

from pickel.app.runtime import RuntimeConversation
from pickel.app.runtime_models import TurnRequest, TurnResult
from pickel.cli.query_output import (
    encode_event_jsonl,
    encode_result_json,
    encode_result_text,
)
from pickel.runs.host_call_types import (
    CONFIRMATION_CALL,
    EXTERNAL_ACTION_CALL,
    STRUCTURED_INPUT_CALL,
    ConfirmationAnswer,
    ExternalActionAnswer,
    StructuredInputAnswer,
)
from pickel.runs.host_calls import HostCallHandlerLease

OutputFormat = Literal["text", "json", "jsonl"]


class NonInteractiveHostCalls:
    """无人值守策略：所有需要人类参与的调用均 fail closed。"""

    @staticmethod
    def attach(conversation: RuntimeConversation) -> tuple[HostCallHandlerLease, ...]:
        router = conversation.runtime_bus.host_calls
        return (
            router.register(
                CONFIRMATION_CALL,
                lambda _request, _context: ConfirmationAnswer(decision="decline"),
            ),
            router.register(
                STRUCTURED_INPUT_CALL,
                lambda _request, _context: StructuredInputAnswer(action="cancel"),
            ),
            router.register(
                EXTERNAL_ACTION_CALL,
                lambda _request, _context: ExternalActionAnswer(action="decline"),
            ),
        )


class QuerySurface:
    def __init__(self, *, stdout: TextIO, output_format: OutputFormat) -> None:
        self.stdout = stdout
        self.output_format = output_format

    async def run(
        self,
        *,
        conversation: RuntimeConversation,
        request: TurnRequest,
    ) -> TurnResult:
        leases = NonInteractiveHostCalls.attach(conversation)
        unsubscribe: Callable[[], None] | None = None
        if self.output_format == "jsonl":
            unsubscribe = conversation.subscribe(self._write_event)
        try:
            result = await conversation.turn(request)
            conversation.flush()
            if self.output_format == "text":
                text = encode_result_text(result)
                if text:
                    self._write(text)
            elif self.output_format == "json":
                self._write(encode_result_json(result))
            return result
        finally:
            if unsubscribe is not None:
                unsubscribe()
            for lease in leases:
                lease.close()
            conversation.archive()

    def _write_event(self, event) -> None:
        self._write(encode_event_jsonl(event))

    def _write(self, value: str) -> None:
        if value:
            self.stdout.write(value)
        self.stdout.write("\n")
        self.stdout.flush()
