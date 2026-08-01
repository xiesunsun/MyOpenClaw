"""CLI 对 Runtime Host call 的具体交互策略。"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any

from pickel.runs.host_call_types import (
    CONFIRMATION_CALL,
    EXTERNAL_ACTION_CALL,
    STRUCTURED_INPUT_CALL,
    ConfirmationAnswer,
    ConfirmationRequest,
    ExternalActionAnswer,
    ExternalActionRequest,
    StructuredInputAnswer,
    StructuredInputRequest,
)
from pickel.runs.host_calls import HostCallContext, HostCallHandlerLease
from pickel.runs.runtime_bus import RuntimeBus

InputReader = Callable[[str], str | Awaitable[str]]
MessageRenderer = Callable[[str], None]


class CliHostCallHandlers:
    """prompt-toolkit/自定义 input reader 适配器；多个请求在 CLI 上串行展示。"""

    def __init__(
        self,
        *,
        input_reader: InputReader,
        render_message: MessageRenderer,
    ) -> None:
        self._input_reader = input_reader
        self._render_message = render_message
        self._input_lock = asyncio.Lock()

    def attach(self, bus: RuntimeBus) -> tuple[HostCallHandlerLease, ...]:
        return (
            bus.host_calls.register(CONFIRMATION_CALL, self.handle_confirmation),
            bus.host_calls.register(
                STRUCTURED_INPUT_CALL,
                self.handle_structured_input,
            ),
            bus.host_calls.register(
                EXTERNAL_ACTION_CALL,
                self.handle_external_action,
            ),
        )

    async def handle_confirmation(
        self,
        request: ConfirmationRequest,
        _context: HostCallContext,
    ) -> ConfirmationAnswer:
        async with self._input_lock:
            self._render_message(f"{request.title}\n{request.message}")
            value = (await self._read("Confirm [y/N] > ")).strip().lower()
            return ConfirmationAnswer(
                decision="accept" if value in {"y", "yes"} else "decline"
            )

    async def handle_structured_input(
        self,
        request: StructuredInputRequest,
        _context: HostCallContext,
    ) -> StructuredInputAnswer:
        async with self._input_lock:
            self._render_message(f"{request.title}\n{request.message}")
            schema = request.schema
            if schema.get("type", "object") != "object":
                return StructuredInputAnswer(action="cancel")
            properties = schema.get("properties")
            if not isinstance(properties, dict):
                properties = {}
            required = {
                item for item in schema.get("required", []) if isinstance(item, str)
            }
            content: dict[str, Any] = {}
            for name, raw_property in properties.items():
                if not isinstance(name, str) or not isinstance(raw_property, dict):
                    continue
                while True:
                    description = raw_property.get("description")
                    suffix = " *" if name in required else ""
                    if isinstance(description, str) and description:
                        self._render_message(description)
                    raw = await self._read(f"{name}{suffix} > ")
                    if not raw.strip() and name not in required:
                        break
                    try:
                        content[name] = _parse_value(raw, raw_property)
                    except ValueError as exc:
                        self._render_message(str(exc))
                        continue
                    break
            return StructuredInputAnswer(action="accept", content=content)

    async def handle_external_action(
        self,
        request: ExternalActionRequest,
        _context: HostCallContext,
    ) -> ExternalActionAnswer:
        async with self._input_lock:
            self._render_message(
                f"{request.title}\n{request.message}\nURL: {request.url}"
            )
            value = (
                (await self._read("Continue external action [y/N] > ")).strip().lower()
            )
            return ExternalActionAnswer(
                action="accept" if value in {"y", "yes"} else "decline"
            )

    async def _read(self, prompt: str) -> str:
        result = self._input_reader(prompt)
        if inspect.isawaitable(result):
            result = await result
        return result


def _parse_value(raw: str, schema: dict[str, Any]) -> Any:
    value = raw.strip()
    choices = schema.get("enum")
    if (
        isinstance(choices, list)
        and choices
        and value not in {str(item) for item in choices}
    ):
        raise ValueError(f"请输入以下值之一: {', '.join(map(str, choices))}")

    value_type = schema.get("type", "string")
    if value_type == "string":
        return raw
    if value_type == "integer":
        try:
            return int(value)
        except ValueError:
            raise ValueError("请输入整数") from None
    if value_type == "number":
        try:
            return float(value)
        except ValueError:
            raise ValueError("请输入数字") from None
    if value_type == "boolean":
        normalized = value.lower()
        if normalized in {"true", "yes", "y", "1"}:
            return True
        if normalized in {"false", "no", "n", "0"}:
            return False
        raise ValueError("请输入 true/false 或 yes/no")
    if value_type in {"array", "object"}:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"请输入合法 JSON: {exc.msg}") from None
        expected = list if value_type == "array" else dict
        if not isinstance(parsed, expected):
            raise ValueError(f"请输入 JSON {value_type}")
        return parsed
    raise ValueError(f"CLI 暂不支持字段类型: {value_type}")
