"""一次待执行的运行期工具调用。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolCall:
    """Provider 输出经协议适配后交给工具执行器的调用参数。"""

    id: str
    name: str
    arguments: dict[str, object]
    thought_signature: bytes | None = None
