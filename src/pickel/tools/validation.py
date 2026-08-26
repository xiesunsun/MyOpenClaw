"""工具输入输出的 JSON Schema 验证。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from jsonschema import SchemaError, ValidationError
from jsonschema.validators import validator_for

from pickel.observe.records import ErrorInfo
from pickel.shared.frozen_json import thaw_json
from pickel.tools.base import BaseTool, ToolExecutionResult


def validate_tool_arguments(tool: BaseTool, arguments: dict[str, Any]) -> str | None:
    """验证模型或 Hook 生成的参数；合法时返回 ``None``。"""
    return validate_json_schema(arguments, tool.spec.input_schema)


def validate_json_schema(value: Any, schema: Mapping[str, Any]) -> str | None:
    """验证冻结定义中的 JSON Schema，不要求加载 Tool 实例。"""
    return _validate(value, schema)


def validate_tool_result(
    tool: BaseTool | Any, result: ToolExecutionResult
) -> str | None:
    """验证工具结果的 JSON 合法性和声明的 output schema。

    ``tool`` 通常是 ``BaseTool``。恢复路径只有冻结的 ``ToolVersion``，因此
    这里也接受拥有 ``output_schema`` 属性的 Tool 定义，避免为验证重建可执行
    Tool 实例。
    """
    if result.structured_content is not None:
        try:
            json.dumps(result.structured_content, allow_nan=False)
        except (TypeError, ValueError) as exc:
            return f"$.structured_content 不是 JSON 数据: {exc}"
    spec = getattr(tool, "spec", tool)
    output_schema = getattr(spec, "output_schema", None)
    if output_schema is None or result.is_error:
        return None
    return validate_json_schema(result.structured_content, output_schema)


def invalid_tool_result(
    result: ToolExecutionResult,
    validation_error: str,
) -> ToolExecutionResult:
    """将非法结果收敛为可持久化、稳定的错误 ToolResult。

    不保留非法 ``structured_content``：后续 ``ToolResultMessage`` 会冻结并写入
    ConversationNode，继续携带它会让错误在提交阶段才爆出。
    """
    return ToolExecutionResult(
        content=f"INVALID_TOOL_OUTPUT: {validation_error}",
        is_error=True,
        metadata={
            **(
                result.metadata
                if isinstance(getattr(result, "metadata", None), dict)
                else {}
            ),
            "validation_error": validation_error,
        },
        error=ErrorInfo(
            kind="tool_output",
            type="INVALID_TOOL_OUTPUT",
            message=validation_error,
            retryable=False,
        ),
    )


def _validate(value: Any, schema: Mapping[str, Any]) -> str | None:
    schema = thaw_json(schema)
    try:
        validator_class = validator_for(schema)
        validator_class.check_schema(schema)
        errors = sorted(
            validator_class(schema).iter_errors(value),
            key=lambda error: list(error.absolute_path),
        )
    except SchemaError as exc:
        return f"工具 JSON Schema 无效: {exc.message}"
    if not errors:
        return None
    error: ValidationError = errors[0]
    path = "$" + "".join(
        f"[{part}]" if isinstance(part, int) else f".{part}"
        for part in error.absolute_path
    )
    return f"{path}: {error.message}"
