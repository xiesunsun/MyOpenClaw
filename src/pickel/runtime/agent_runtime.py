"""接受并驱动 SessionOperation 的核心运行引擎。"""

from __future__ import annotations

from pickel.conversations.agent_message import UserMessage
from pickel.operations.operation_service import OperationService
from pickel.runtime.operation_driver import (
    OperationDriveResult,
    OperationDriver,
    StreamDeltaConsumer,
)
from pickel.runtime.runtime_bindings import RuntimeBindings
from pickel.runs.host_calls import HostCallClient


class AgentRuntime:
    """一个冻结 AgentPackageVersion 的可组合运行内核。"""

    def __init__(
        self,
        *,
        bindings: RuntimeBindings,
        operation_service: OperationService,
        operation_driver: OperationDriver,
    ) -> None:
        self._bindings = bindings
        self._operation_service = operation_service
        self._operation_driver = operation_driver

    @property
    def bindings(self) -> RuntimeBindings:
        return self._bindings

    async def start_agent_run(
        self,
        *,
        session_id: str,
        user_message: UserMessage,
        host_calls: HostCallClient | None = None,
        consume_delta: StreamDeltaConsumer | None = None,
    ) -> OperationDriveResult:
        accepted = self._operation_service.accept_agent_run(
            session_id=session_id,
            agent_package_version_id=(
                self._bindings.agent_package_version.package_version_id
            ),
            user_message=user_message,
        )
        return await self._operation_driver.drive_operation(
            accepted.operation.operation_id,
            host_calls=host_calls,
            consume_delta=consume_delta,
        )

    async def resume_operation(
        self,
        operation_id: str,
        *,
        host_calls: HostCallClient | None = None,
        consume_delta: StreamDeltaConsumer | None = None,
    ) -> OperationDriveResult:
        operation = self._operation_service.load_session_operation(operation_id)
        if (
            operation.agent_package_version_id
            != self._bindings.agent_package_version.package_version_id
        ):
            raise ValueError("AgentRuntime Package 与 SessionOperation 绑定版本不匹配")
        return await self._operation_driver.drive_operation(
            operation_id,
            host_calls=host_calls,
            consume_delta=consume_delta,
        )

    async def start_delegated_run(
        self,
        *,
        parent_operation_id: str,
        parent_step_id: str,
        user_message: UserMessage,
        parent_tool_call_id: str | None = None,
        host_calls: HostCallClient | None = None,
        consume_delta: StreamDeltaConsumer | None = None,
    ) -> OperationDriveResult:
        accepted = self._operation_service.start_delegated_run(
            agent_package_version_id=(
                self._bindings.agent_package_version.package_version_id
            ),
            user_message=user_message,
            parent_operation_id=parent_operation_id,
            parent_step_id=parent_step_id,
            parent_tool_call_id=parent_tool_call_id,
        )
        return await self._operation_driver.drive_operation(
            accepted.accepted_run.operation.operation_id,
            host_calls=host_calls,
            consume_delta=consume_delta,
        )
