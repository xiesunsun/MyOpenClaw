"""观测平台轨迹值对象（设计 §4）。

只读派生、可丢可重算；全部字段 JSON-ready，供 HTML 内嵌数据岛使用。
真源始终是 Session entry——本模块不承载任何独立事实。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolExecution:
    tool_call_id: str
    name: str
    arguments: dict[str, Any]
    result_preview: str = ""
    is_error: bool = False
    # 孤儿 tool_result（找不到对应 call）保留并标注，不丢数据。
    orphan: bool = False
    # 以下三项来自 trace 增强，非真源；trace 缺失时为 None。
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class Step:
    """一次 generate = 一条 assistant entry。"""

    index: int
    thinking_chars: int
    text: str
    tool_executions: list[ToolExecution]
    model_label: str
    finish_reason: str | None
    usage: dict[str, int]
    elapsed_ms: int | None
    hook_injected_chars: int | None
    context_fingerprint: str | None


@dataclass(frozen=True)
class Turn:
    """一条 user 消息到下一条 user 之前。"""

    index: int
    query: str
    steps: list[Step]
    final_text: str
    usage_totals: dict[str, int]
    elapsed_ms: int
    # trace 增强，非真源。
    started_at: str | None = None
    failed: dict[str, str] | None = None
    interrupted: bool = False


@dataclass(frozen=True)
class SessionTrajectory:
    session_id: str
    agent_id: str
    cwd: str
    title: str | None
    created_at: str
    updated_at: str
    turns: list[Turn] = field(default_factory=list)
    # 全会话 step 序号处出现过 compaction（上下文曲线的竖线标记位）。
    compaction_steps: list[int] = field(default_factory=list)
    session_usage: dict[str, int] = field(default_factory=dict)
    trace_available: bool = False


def trajectory_to_dict(trajectory: SessionTrajectory) -> dict[str, Any]:
    return asdict(trajectory)
