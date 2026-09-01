"""把 Provider SDK 响应投影成可持久化 JSON object。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from types import SimpleNamespace
from typing import Any

_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "access_token",
        "refresh_token",
        "password",
        "secret",
        "credential",
        "credentials",
        "headers",
    }
)


def provider_response_json(value: Any) -> dict[str, Any]:
    """保留 Provider 明确返回字段，同时拒绝不可 JSON 化的资源对象。"""
    projected = _json_value(value)
    if not isinstance(projected, dict):
        raise TypeError("Provider 完整响应必须可以投影为 JSON object")
    return projected


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name.lower() in _SECRET_KEYS:
                raise TypeError(f"Provider 响应包含禁止持久化的敏感字段: {name}")
            result[name] = _json_value(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    if hasattr(value, "model_dump"):
        return _json_value(
            value.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
    if is_dataclass(value):
        return _json_value(asdict(value))
    # 测试 double 和少数标准库返回值可安全按显式字段投影；不再遍历
    # 任意 SDK 对象的 __dict__，避免把内部凭证或连接信息写入观测数据。
    if type(value) is SimpleNamespace:
        return _json_value(vars(value))
    raise TypeError(f"Provider 响应字段不可 JSON 化: {type(value).__name__}")
