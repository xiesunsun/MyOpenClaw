from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from myopenclaw.application.contracts import ToolSpec
from myopenclaw.application.ports import ToolExecutorPort
from myopenclaw.domain.agent import Agent
from myopenclaw.domain.message import ToolCall, ToolCallResult
from myopenclaw.shared.file_access import FileAccessMode
from myopenclaw.infrastructure.tools.base import BaseTool, ToolExecutionContext
from myopenclaw.infrastructure.tools.catalog import builtin_tools
from myopenclaw.infrastructure.tools.file_service import WorkspaceFileService
from myopenclaw.infrastructure.tools.policy import (
    FileAccessPolicy,
    FullAccessPathPolicy,
    WorkspacePathAccessPolicy,
)
from myopenclaw.infrastructure.tools.registry import ToolRegistry
from myopenclaw.infrastructure.tools.shell import ShellSessionManager


@dataclass
class BuiltinToolExecutor(ToolExecutorPort):
    registry: ToolRegistry = field(
        default_factory=lambda: ToolRegistry(tools=builtin_tools())
    )
    shell_session_manager: ShellSessionManager = field(default_factory=ShellSessionManager)

    def describe_tools(self, tool_ids: list[str]) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=tool.spec.name,
                description=tool.spec.description,
                input_schema=dict(tool.spec.input_schema),
                output_schema=(
                    dict(tool.spec.output_schema)
                    if tool.spec.output_schema is not None
                    else None
                ),
            )
            for tool in self.registry.resolve_many(tool_ids)
        ]

    async def execute_calls(
        self,
        *,
        tool_calls: list[ToolCall],
        agent: Agent,
        session_id: str,
    ) -> list[ToolCallResult]:
        tools_by_name = {
            tool.spec.name: tool for tool in self.registry.resolve_many(agent.tool_ids)
        }
        tasks = [
            asyncio.create_task(
                self._execute_call(
                    tool_call=tool_call,
                    tool=tools_by_name.get(tool_call.name),
                    agent=agent,
                    session_id=session_id,
                )
            )
            for tool_call in tool_calls
        ]
        return [await task for task in tasks]

    async def _execute_call(
        self,
        *,
        tool_call: ToolCall,
        tool: BaseTool | None,
        agent: Agent,
        session_id: str,
    ) -> ToolCallResult:
        if tool is None:
            return ToolCallResult(
                call_id=tool_call.id,
                content=f"Tool '{tool_call.name}' is not available.",
                is_error=True,
            )

        context = ToolExecutionContext(
            agent_id=agent.agent_id,
            session_id=session_id,
            workspace_path=agent.workspace,
            workspace_files=WorkspaceFileService(
                workspace_root=agent.workspace,
                access_policy=self._policy_for_mode(agent.file_access_mode),
            ),
            shell_session_manager=self.shell_session_manager,
        )
        try:
            result = await tool.execute(tool_call.arguments, context)
        except Exception as exc:
            return ToolCallResult(
                call_id=tool_call.id,
                content=f"Tool '{tool_call.name}' failed: {exc}",
                is_error=True,
            )
        return ToolCallResult(
            call_id=tool_call.id,
            content=result.content,
            is_error=result.is_error,
            metadata=dict(result.metadata),
        )

    @staticmethod
    def _policy_for_mode(mode: str) -> FileAccessPolicy:
        if mode == FileAccessMode.FULL.value:
            return FullAccessPathPolicy()
        return WorkspacePathAccessPolicy()
