"""工具运行期服务容器。

宿主提供给进程内工具的服务。extension 工具也在进程内跑，但它在装载时
用闭包持有自己的依赖，只从这里取宿主服务；服务种类由 core 决定、数量有限，
一个字段明确的 dataclass 就够，不做「能力声明 + 按需注入」。
S2 沙箱化时从这里替换实现即可，工具侧代码不动。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pickel.conversations.agent_message import UserMessage
    from pickel.inbox.message import InboxMessage
    from pickel.operations.agent_delegation import AgentDelegation
    from pickel.operations.delegation_service import ChildAgentSnapshot
    from pickel.conversations.agent_message import AssistantMessage

if TYPE_CHECKING:  # 运行期不导入，避免 base ↔ shell / file_service 循环
    from pickel.artifacts.artifact_service import ArtifactService
    from pickel.runtime.host_calls import HostCallClient
    from pickel.skills.store import SkillStore
    from pickel.tools.file_service import WorkspaceFileService
    from pickel.tools.shell import BashOperations


class DelegationControl(Protocol):
    """工具使用的 durable delegation 窄接口。"""

    async def start_delegation(
        self,
        *,
        parent_operation_id: str,
        parent_step_id: str,
        parent_tool_call_id: str,
        message: UserMessage,
    ) -> "AgentDelegation": ...

    async def send_parent_followup(
        self,
        *,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        target_child_session_id: str,
        message: UserMessage,
    ) -> "InboxMessage": ...

    async def list_child_agents(
        self,
        *,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
    ) -> tuple["ChildAgentSnapshot", ...]: ...

    async def send_child_report(
        self,
        *,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        output: str,
    ) -> "InboxMessage": ...

    async def interrupt_agent(
        self,
        *,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        target_child_session_id: str,
    ) -> str | None: ...

    async def cancel_delegation(
        self,
        *,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        target_child_session_id: str,
    ) -> str | None:
        """旧 Package 迁移兼容入口；新 Package 不公开此名称。"""
        ...

    async def wait_delegation(
        self,
        *,
        sender_operation_id: str,
        sender_step_id: str,
        sender_tool_call_id: str,
        target_child_session_id: str,
        timeout_seconds: float,
    ) -> tuple["ChildAgentSnapshot", "AssistantMessage | None", bool]: ...


@dataclass(frozen=True)
class ToolServices:
    workspace_files: "WorkspaceFileService | None" = None
    bash: "BashOperations | None" = None
    skill_store: "SkillStore | None" = None
    host_calls: "HostCallClient | None" = None
    artifact_service: "ArtifactService | None" = None
    delegation: DelegationControl | None = None
