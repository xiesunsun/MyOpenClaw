"""Turn / Step 瘦运行态。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from myopenclaw.context.hook_feedback import HookFeedback
from myopenclaw.tools.base import ToolExecutionResult


@dataclass
class StepState:
    step_index: int
    status: str = "running"
    assistant_entry_id: str | None = None
    pending_tool_call_ids: list[str] = field(default_factory=list)
    completed_tool_call_ids: list[str] = field(default_factory=list)


@dataclass
class TurnState:
    turn_id: str = field(default_factory=lambda: str(uuid4()))
    status: str = "running"
    current_user_entry_id: str | None = None
    current_step: StepState | None = None
    hook_feedback: list[HookFeedback] = field(default_factory=list)
    final_assistant_entry_id: str | None = None
    # 本 step 新增反馈（Assembler 只注入这份）
    step_hook_feedback: list[HookFeedback] = field(default_factory=list)

    def hook_feedback_for_current_step(self) -> list[HookFeedback]:
        return list(self.step_hook_feedback)

    def begin_step(self, step_index: int) -> StepState:
        self.step_hook_feedback = []
        self.current_step = StepState(step_index=step_index)
        return self.current_step


@dataclass
class ToolExecutionOutcome:
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    result: ToolExecutionResult
    call_index: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
