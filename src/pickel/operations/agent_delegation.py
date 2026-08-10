"""父 AgentRun 与被委派 AgentRun 的不可变关系。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AgentDelegation:
    delegation_id: str
    parent_operation_id: str
    parent_step_id: str
    parent_tool_call_id: str | None
    child_operation_id: str
    child_session_id: str
    created_commit_sequence: int
    created_at: datetime

    def __post_init__(self) -> None:
        if not self.delegation_id:
            raise ValueError("delegation_id 不能为空")
        if not self.parent_operation_id or not self.child_operation_id:
            raise ValueError("AgentDelegation 父子 operation_id 不能为空")
        if self.parent_operation_id == self.child_operation_id:
            raise ValueError("AgentDelegation 不能指向自身")
        if not self.parent_step_id:
            raise ValueError("parent_step_id 不能为空")
        if not self.child_session_id:
            raise ValueError("child_session_id 不能为空")
        if self.created_commit_sequence < 1:
            raise ValueError("created_commit_sequence 必须大于 0")
