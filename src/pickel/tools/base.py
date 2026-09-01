from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, TypeAlias, Union

from pickel.conversations.content_blocks import ToolResultContent
from pickel.conversations.content_blocks import TextBlock
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.shared.frozen_json import freeze_json_object
from pickel.tools.services import ToolServices

JSONValue: TypeAlias = (
    str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]
)
ToolFunctionResult = Union[Awaitable[JSONValue], JSONValue]
ToolFunction = Callable[[dict[str, Any], "ToolExecutionContext"], ToolFunctionResult]
ToolRenderer = Callable[[JSONValue], tuple[ToolResultContent, ...]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    replay_policy: Literal["safe", "never"] = "never"

    def __post_init__(self) -> None:
        if self.replay_policy not in {"safe", "never"}:
            raise ValueError("ToolSpec.replay_policy 必须是 safe 或 never")
        if not isinstance(self.output_schema, dict):
            raise TypeError("ToolSpec.output_schema 必须是 JSON Schema object")
        object.__setattr__(self, "input_schema", freeze_json_object(self.input_schema))
        object.__setattr__(
            self, "output_schema", freeze_json_object(self.output_schema)
        )


@dataclass(frozen=True)
class ToolExecutionContext:
    agent_id: str
    identity: ExecutionIdentity
    workspace_path: Path
    services: ToolServices = field(default_factory=ToolServices)


class ToolExecutionError(RuntimeError):
    """工具执行失败；失败不是另一种成功返回值。"""


class BaseTool:
    spec: ToolSpec

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> JSONValue:
        raise NotImplementedError

    def render(self, validated_value: JSONValue) -> tuple[ToolResultContent, ...]:
        """纯地把已校验 JSON value 转成模型可见内容。"""
        if isinstance(validated_value, str):
            text = validated_value
        else:
            text = json.dumps(
                validated_value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        return (TextBlock(text=text),)


class FunctionTool(BaseTool):
    def __init__(
        self,
        *,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        output_schema: dict[str, Any],
        func: ToolFunction,
        renderer: ToolRenderer | None = None,
        replay_policy: Literal["safe", "never"] = "never",
    ) -> None:
        self.spec = ToolSpec(
            name=name,
            description=description,
            input_schema=input_schema,
            output_schema=output_schema,
            replay_policy=replay_policy,
        )
        self._func = func
        self._renderer = renderer
        self._signature = inspect.signature(func)

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> JSONValue:
        call_kwargs = self._build_call_kwargs(arguments, context)
        result = self._func(**call_kwargs)
        if inspect.isawaitable(result):
            result = await result
        return result

    def render(self, validated_value: JSONValue) -> tuple[ToolResultContent, ...]:
        if self._renderer is not None:
            return self._renderer(validated_value)
        return super().render(validated_value)

    def _build_call_kwargs(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> dict[str, Any]:
        call_kwargs: dict[str, Any] = {}
        parameters = self._signature.parameters
        for name in parameters:
            if name == "context":
                call_kwargs[name] = context
            elif name == "arguments":
                call_kwargs[name] = arguments
            elif name in arguments:
                call_kwargs[name] = arguments[name]
        return call_kwargs


def tool(
    *,
    name: str,
    description: str,
    input_schema: dict[str, Any] | None = None,
    parameters: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    render: ToolRenderer | None = None,
    replay_policy: Literal["safe", "never"] = "never",
) -> Callable[[ToolFunction], FunctionTool]:
    schema = input_schema or parameters
    if schema is None:
        raise ValueError("tool() requires either input_schema or parameters")
    if output_schema is None:
        raise ValueError("tool() requires output_schema")

    def decorator(func: ToolFunction) -> FunctionTool:
        return FunctionTool(
            name=name,
            description=description,
            input_schema=schema,
            output_schema=output_schema,
            func=func,
            renderer=render,
            replay_policy=replay_policy,
        )

    return decorator
