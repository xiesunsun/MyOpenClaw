from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Union

from pickel.observe.records import ErrorInfo
from pickel.conversations.content_blocks import ToolResultContent
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.tools.services import ToolServices

ToolFunctionResult = Union[Awaitable["ToolExecutionResult"], "ToolExecutionResult"]
ToolFunction = Callable[[dict[str, Any], "ToolExecutionContext"], ToolFunctionResult]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class ToolExecutionContext:
    agent_id: str
    identity: ExecutionIdentity
    workspace_path: Path
    services: ToolServices = field(default_factory=ToolServices)


@dataclass
class ToolExecutionResult:
    """工具执行结果。

    ``content``、``content_blocks``、``structured_content`` 和 ``is_error``
    构成模型可见合同；``metadata`` 与 ``error`` 只服务 Runtime 和观测系统。
    """

    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    error: ErrorInfo | None = None
    # 新字段放在末尾，避免破坏外部 extension 的位置参数调用。
    content_blocks: list[ToolResultContent] = field(default_factory=list)
    structured_content: Any | None = None

    def __post_init__(self) -> None:
        if self.error is not None:
            self.is_error = True


class BaseTool:
    spec: ToolSpec

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        raise NotImplementedError


class FunctionTool(BaseTool):
    def __init__(
        self,
        *,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        func: ToolFunction,
    ) -> None:
        self.spec = ToolSpec(
            name=name,
            description=description,
            input_schema=input_schema,
        )
        self._func = func
        self._signature = inspect.signature(func)

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        call_kwargs = self._build_call_kwargs(arguments, context)
        result = self._func(**call_kwargs)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, ToolExecutionResult):
            return result
        return ToolExecutionResult(content=str(result))

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
    input_schema: Optional[dict[str, Any]] = None,
    parameters: Optional[dict[str, Any]] = None,
) -> Callable[[ToolFunction], FunctionTool]:
    schema = input_schema or parameters
    if schema is None:
        raise ValueError("tool() requires either input_schema or parameters")

    def decorator(func: ToolFunction) -> FunctionTool:
        return FunctionTool(
            name=name,
            description=description,
            input_schema=schema,
            func=func,
        )

    return decorator
