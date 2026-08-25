"""Runtime 各执行边界共用的稳定身份。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionIdentity:
    """引用一次 Session 执行中的 Operation、Step、ToolCall 或 Message。"""

    session_id: str = ""
    operation_id: str | None = None
    step_id: str | None = None
    step_sequence: int | None = None
    tool_call_id: str | None = None
    message_id: str | None = None
