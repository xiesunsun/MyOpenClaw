"""工具输入输出的 JSON Schema 验证。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from jsonschema import SchemaError, ValidationError
from jsonschema.validators import validator_for

from pickel.shared.frozen_json import thaw_json
from pickel.tools.base import BaseTool, ToolExecutionResult


def validate_tool_arguments(tool: BaseTool, arguments: dict[str, Any]) -> str | None:
    """验证模型或 Hook 生成的参数；合法时返回 ``None``。"""
    return validate_json_schema(arguments, tool.spec.input_schema)


def validate_json_schema(value: Any, schema: Mapping[str, Any]) -> str | None:
    """验证冻结定义中的 JSON Schema，不要求加载 Tool 实例。"""
    return _validate(value, schema)


def validate_tool_result(tool: BaseTool, result: ToolExecutionResult) -> str | None:
    """有 output_schema 时验证模型可见的结构化结果。"""
    if result.structured_content is not None:
        try:
            json.dumps(result.structured_content)
        except (TypeError, ValueError) as exc:
            return f"$.structured_content 不是 JSON 数据: {exc}"
    if tool.spec.output_schema is None or result.is_error:
        return None
    return validate_json_schema(result.structured_content, tool.spec.output_schema)


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
