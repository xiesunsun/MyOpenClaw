"""JSON 值的递归冻结与可序列化展开。"""

from __future__ import annotations

import math
from types import MappingProxyType
from typing import Any, Mapping, TypeAlias

JSONScalar: TypeAlias = str | int | float | bool | None
FrozenJSON: TypeAlias = (
    JSONScalar | tuple["FrozenJSON", ...] | Mapping[str, "FrozenJSON"]
)


def freeze_json(value: Any) -> FrozenJSON:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON number 不能是 NaN 或 Infinity")
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object key 必须是字符串")
            result[key] = freeze_json(item)
        return MappingProxyType(result)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    raise TypeError("值必须只包含 JSON 类型")


def freeze_json_object(value: Mapping[str, Any]) -> Mapping[str, FrozenJSON]:
    if not isinstance(value, Mapping):
        raise TypeError("JSON object 必须是 mapping")
    return freeze_json(value)


def thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json(item) for item in value]
    return value
