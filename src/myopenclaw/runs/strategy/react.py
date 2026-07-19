"""ReAct：ModelContext + 分 checkpoint 落盘。"""

from __future__ import annotations

import asyncio
import inspect
import time
from numbers import Real
from uuid import uuid4

from myopenclaw.context.model_context import SystemContent, ToolDefinition
from myopenclaw.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ToolResultMessage,
)
from myopenclaw.conversations.content_blocks import TextContent, ToolCallContent
from myopenclaw.conversations.message import ToolCall
from myopenclaw.conversations.metadata import MessageMetadata
from myopenclaw.conversations.session import Session
from myopenclaw.runs.dependencies import RunDependencies
from myopenclaw.runs.events import RuntimeEvent, RuntimeEventType
from myopenclaw.runs.strategy.base import ExecutionStrategy, RuntimeEventHandler
from myopenclaw.runs.turn_state import ToolExecutionOutcome, TurnState
from myopenclaw.tools.base import ToolExecutionResult


class ReActStrategy(ExecutionStrategy):
    """Reason+Act：工具副作用前先落盘 assistant intent。"""

    DEFAULT_PROVIDER_TIMEOUT_SECONDS = 600.0

    def __init__(self, max_steps: int = 8) -> None:
        self.max_steps = max_steps

    async def execute(
        self,
        deps: RunDependencies,
        session: Session,
        event_handler: RuntimeEventHandler | None = None,
    ) -> AssistantMessage:
        turn = TurnState()
        last_assistant: AssistantMessage | None = None

        system = SystemContent.from_text(deps.agent.system_instruction or "")
        tools = [
            ToolDefinition(
                name=tool.spec.name,
                description=tool.spec.description,
                input_schema=tool.spec.input_schema,
            )
            for tool in deps.tools
        ]

        for step_index in range(1, self.max_steps + 1):
            step = turn.begin_step(step_index)
            await self._emit_event(
                event_handler,
                RuntimeEvent(
                    event_type=RuntimeEventType.MODEL_STEP_STARTED,
                    step_index=step_index,
                ),
            )

            model_context = deps.context_assembler.assemble(
                entries=session.active_path(),
                system=system,
                tools=tools,
                hook_feedback=turn.hook_feedback_for_current_step(),
                unit_window=deps.unit_window,
            )

            start = time.perf_counter()
            assistant = await self._generate_with_optional_timeout(
                deps=deps,
                context=model_context,
            )
            elapsed_ms = round((time.perf_counter() - start) * 1000)
            assistant = self._ensure_metadata(deps, assistant, elapsed_ms)
            last_assistant = assistant

            # checkpoint BEFORE tools
            entry = session.append_assistant(assistant)
            step.assistant_entry_id = entry.entry_id
            self._flush_entry(deps, session, entry)

            tool_calls = [
                block
                for block in assistant.content
                if isinstance(block, ToolCallContent)
            ]
            if not tool_calls:
                text = self._assistant_text(assistant)
                await self._emit_event(
                    event_handler,
                    RuntimeEvent(
                        event_type=RuntimeEventType.ASSISTANT_MESSAGE,
                        step_index=step_index,
                        text=text,
                        metadata=self._to_message_metadata(assistant),
                    ),
                )
                turn.final_assistant_entry_id = entry.entry_id
                turn.status = "completed"
                return assistant

            step.pending_tool_call_ids = [call.id for call in tool_calls]
            batch_id = uuid4().hex
            outcomes = await self._execute_tool_batch(
                batch_id=batch_id,
                step_index=step_index,
                tool_calls=tool_calls,
                deps=deps,
                session=session,
                event_handler=event_handler,
            )
            # 按调用顺序串行提交 tool result
            ordered = sorted(outcomes, key=lambda item: item.call_index)
            for outcome in ordered:
                result_entry = session.append_tool_result(
                    ToolResultMessage(
                        tool_call_id=outcome.tool_call_id,
                        tool_name=outcome.tool_name,
                        content=[TextContent(text=outcome.result.content)],
                        is_error=outcome.result.is_error,
                    )
                )
                self._flush_entry(deps, session, result_entry)
                step.completed_tool_call_ids.append(outcome.tool_call_id)

        # max steps
        max_msg = AssistantMessage(
            content=[
                TextContent(text="Reached the maximum number of reasoning steps.")
            ],
            metadata=last_assistant.metadata if last_assistant else None,
        )
        entry = session.append_assistant(max_msg)
        self._flush_entry(deps, session, entry)
        await self._emit_event(
            event_handler,
            RuntimeEvent(
                event_type=RuntimeEventType.ASSISTANT_MESSAGE,
                step_index=self.max_steps,
                text="Reached the maximum number of reasoning steps.",
                metadata=self._to_message_metadata(max_msg),
            ),
        )
        return max_msg

    def _flush_entry(self, deps: RunDependencies, session: Session, entry) -> None:
        if deps.session_service is None:
            return
        deps.session_service.flush_new_entries(session=session, entries=[entry])

    async def _generate_with_optional_timeout(self, *, deps: RunDependencies, context):
        timeout_seconds = self._provider_timeout_seconds(deps)
        if timeout_seconds is None:
            return await deps.provider.generate(context)
        return await asyncio.wait_for(
            deps.provider.generate(context),
            timeout=timeout_seconds,
        )

    @staticmethod
    def _provider_timeout_seconds(deps: RunDependencies) -> float | None:
        timeout_seconds = deps.agent.model_config.provider_options.get(
            "timeout_seconds"
        )
        if timeout_seconds is None:
            return ReActStrategy.DEFAULT_PROVIDER_TIMEOUT_SECONDS
        if not isinstance(timeout_seconds, Real):
            return ReActStrategy.DEFAULT_PROVIDER_TIMEOUT_SECONDS
        timeout_value = float(timeout_seconds)
        if timeout_value <= 0:
            return ReActStrategy.DEFAULT_PROVIDER_TIMEOUT_SECONDS
        return timeout_value

    async def _execute_tool_batch(
        self,
        *,
        batch_id: str,
        step_index: int,
        tool_calls: list[ToolCallContent],
        deps: RunDependencies,
        session: Session,
        event_handler: RuntimeEventHandler | None,
    ) -> list[ToolExecutionOutcome]:
        total_calls = len(tool_calls)
        runtime_calls = [
            ToolCall(
                id=call.id,
                name=call.name,
                arguments=call.arguments,
            )
            for call in tool_calls
        ]
        for call_index, tool_call in enumerate(runtime_calls):
            await self._emit_event(
                event_handler,
                RuntimeEvent(
                    event_type=RuntimeEventType.TOOL_CALL_STARTED,
                    step_index=step_index,
                    batch_id=batch_id,
                    call_index=call_index,
                    total_calls=total_calls,
                    tool_call=tool_call,
                ),
            )

        tasks = [
            asyncio.create_task(
                self._execute_one(
                    call_index=call_index,
                    tool_call=tool_call,
                    deps=deps,
                    session=session,
                )
            )
            for call_index, tool_call in enumerate(tool_calls)
        ]
        outcomes: list[ToolExecutionOutcome] = []
        try:
            for completed in asyncio.as_completed(tasks):
                outcome = await completed
                outcomes.append(outcome)
                runtime_call = ToolCall(
                    id=outcome.tool_call_id,
                    name=outcome.tool_name,
                    arguments=outcome.arguments,
                )
                await self._emit_event(
                    event_handler,
                    RuntimeEvent(
                        event_type=(
                            RuntimeEventType.TOOL_CALL_FAILED
                            if outcome.result.is_error
                            else RuntimeEventType.TOOL_CALL_COMPLETED
                        ),
                        step_index=step_index,
                        batch_id=batch_id,
                        call_index=outcome.call_index,
                        total_calls=total_calls,
                        tool_call=runtime_call,
                        tool_result=outcome.result,
                    ),
                )
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
        return outcomes

    async def _execute_one(
        self,
        *,
        call_index: int,
        tool_call: ToolCallContent,
        deps: RunDependencies,
        session: Session,
    ) -> ToolExecutionOutcome:
        result = await self._execute_tool_call(deps=deps, session=session, tool_call=tool_call)
        return ToolExecutionOutcome(
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            arguments=dict(tool_call.arguments),
            result=result,
            call_index=call_index,
        )

    async def _execute_tool_call(
        self,
        *,
        deps: RunDependencies,
        session: Session,
        tool_call: ToolCallContent,
    ) -> ToolExecutionResult:
        tool = next(
            (candidate for candidate in deps.tools if candidate.spec.name == tool_call.name),
            None,
        )
        if tool is None:
            return ToolExecutionResult(
                content=f"Tool '{tool_call.name}' is not available.",
                is_error=True,
            )
        exec_context = deps.get_tool_execution_context(session.session_id)
        try:
            return await tool.execute(tool_call.arguments, exec_context)
        except Exception as exc:  # noqa: BLE001 — 工具失败转错误结果
            return ToolExecutionResult(
                content=f"Tool '{tool_call.name}' failed: {exc}",
                is_error=True,
            )

    @staticmethod
    def _assistant_text(assistant: AssistantMessage) -> str:
        parts = [
            block.text
            for block in assistant.content
            if isinstance(block, TextContent) and block.text
        ]
        return "\n".join(parts)

    @staticmethod
    def _ensure_metadata(
        deps: RunDependencies,
        assistant: AssistantMessage,
        elapsed_ms: int,
    ) -> AssistantMessage:
        if assistant.metadata is not None:
            # frozen dataclass: rebuild if elapsed missing
            meta = assistant.metadata
            if meta.elapsed_ms is None:
                meta = ModelResponseMetadata(
                    provider=meta.provider,
                    model=meta.model,
                    provider_model_version=meta.provider_model_version,
                    provider_response_id=meta.provider_response_id,
                    finish_reason=meta.finish_reason,
                    finish_message=meta.finish_message,
                    elapsed_ms=elapsed_ms,
                    usage=meta.usage,
                )
                return AssistantMessage(content=list(assistant.content), metadata=meta)
            return assistant
        return AssistantMessage(
            content=list(assistant.content),
            metadata=ModelResponseMetadata(
                provider=deps.agent.model_config.provider,
                model=deps.agent.model_config.model,
                elapsed_ms=elapsed_ms,
            ),
        )

    @staticmethod
    def _to_message_metadata(assistant: AssistantMessage) -> MessageMetadata | None:
        meta = assistant.metadata
        if meta is None:
            return None
        usage = meta.usage
        return MessageMetadata(
            provider=meta.provider,
            model=meta.model,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            total_tokens=usage.total_tokens if usage else None,
            elapsed_ms=meta.elapsed_ms,
            provider_finish_reason=meta.finish_reason,
            provider_finish_message=meta.finish_message,
            provider_response_id=meta.provider_response_id,
            provider_model_version=meta.provider_model_version,
        )

    async def _emit_event(
        self,
        event_handler: RuntimeEventHandler | None,
        event: RuntimeEvent,
    ) -> None:
        if event_handler is None:
            return
        result = event_handler(event)
        if inspect.isawaitable(result):
            await result
