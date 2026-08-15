"""AgentRun 推进过程对应用层公开的进度通知。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from pickel.operations.agent_run_state import ToolCallState
from pickel.tools.base import ToolExecutionResult


@dataclass(frozen=True)
class ModelStepStartedProgress:
    operation_id: str
    step_id: str
    step_sequence: int


@dataclass(frozen=True)
class ToolCallStartedProgress:
    operation_id: str
    step_id: str
    step_sequence: int
    tool_call: ToolCallState
    call_index: int
    total_calls: int


@dataclass(frozen=True)
class ToolCallCompletedProgress(ToolCallStartedProgress):
    result: ToolExecutionResult


AgentRunProgress = (
    ModelStepStartedProgress | ToolCallStartedProgress | ToolCallCompletedProgress
)
AgentRunProgressConsumer = Callable[
    [AgentRunProgress],
    None | Awaitable[None],
]
