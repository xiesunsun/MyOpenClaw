import asyncio
import inspect
import time
from dataclasses import dataclass
from uuid import uuid4

from myopenclaw.conversations.message import (
    SessionMessage,
    ToolCall,
    ToolCallBatch,
    ToolCallResult,
)
from myopenclaw.conversations.metadata import MessageMetadata
from myopenclaw.conversations.session import Session
from myopenclaw.runs.context import AgentRuntimeContext
from myopenclaw.runs.events import RuntimeEvent, RuntimeEventType
from myopenclaw.shared.generation import FinishReason, GenerateRequest, GenerateResult
from myopenclaw.runs.strategy.base import ExecutionStrategy, RuntimeEventHandler
from myopenclaw.tools.base import ToolExecutionResult


@dataclass(frozen=True)
class ToolCallOutcome:
    batch_id: str
    call_index: int
    total_calls: int
    tool_call: ToolCall
    result: ToolExecutionResult


class ReActStrategy(ExecutionStrategy):
    """Reason+Act (ReAct) execution strategy."""

    def __init__(self, max_steps: int = 8) -> None:
        self.max_steps = max_steps

    async def execute(
        self,
        context: AgentRuntimeContext,
        session: Session,
        session_recall_message: SessionMessage | None = None,
        event_handler: RuntimeEventHandler | None = None,
    ) -> GenerateResult:
        last_metadata: MessageMetadata | None = None

        for step_index in range(1, self.max_steps + 1):
            prompt_messages = context.conversation_context_service.build_prompt_messages_from_session(
                session,
                session_recall_message=session_recall_message,
            )
            await self._emit_event(
                event_handler,
                RuntimeEvent(
                    event_type=RuntimeEventType.MODEL_STEP_STARTED,
                    step_index=step_index,
                ),
            )
            start = time.perf_counter()
            result = await context.provider.generate(
                GenerateRequest(
                    system_instruction=context.agent.system_instruction or None,
                    messages=prompt_messages,
                    tools=[tool.spec for tool in context.tools],
                )
            )
            elapsed_ms = round((time.perf_counter() - start) * 1000)
            metadata = result.metadata or MessageMetadata(
                provider=context.agent.model_config.provider,
                model=context.agent.model_config.model,
                input_tokens=result.usage.input_tokens if result.usage else None,
                output_tokens=result.usage.output_tokens if result.usage else None,
                total_tokens=result.usage.total_tokens if result.usage else None,
                elapsed_ms=elapsed_ms,
                provider_finish_reason=result.provider_finish_reason,
                provider_finish_message=result.provider_finish_message,
                provider_response_id=result.provider_response_id,
                provider_model_version=result.provider_model_version,
            )
            result.metadata = metadata
            last_metadata = metadata

            if result.tool_calls:
                batch_id = uuid4().hex
                outcomes = await self._execute_tool_batch(
                    batch_id=batch_id,
                    step_index=step_index,
                    tool_calls=result.tool_calls,
                    context=context,
                    session=session,
                    event_handler=event_handler,
                )
                ordered_outcomes = sorted(outcomes, key=lambda outcome: outcome.call_index)
                session.append_assistant_tool_batch(
                    batch=ToolCallBatch(
                        batch_id=batch_id,
                        step_index=step_index,
                        calls=list(result.tool_calls),
                        results=[
                            ToolCallResult(
                                call_id=outcome.tool_call.id,
                                content=outcome.result.content,
                                is_error=outcome.result.is_error,
                                metadata=dict(outcome.result.metadata),
                            )
                            for outcome in ordered_outcomes
                        ],
                    ),
                    content=result.text,
                    metadata=metadata,
                    provider_thinking_blocks=result.provider_thinking_blocks,
                )
                continue

            session.append_assistant_message(
                result.text,
                metadata=metadata,
                provider_thinking_blocks=result.provider_thinking_blocks,
            )
            await self._emit_event(
                event_handler,
                RuntimeEvent(
                    event_type=RuntimeEventType.ASSISTANT_MESSAGE,
                    step_index=step_index,
                    text=result.text,
                    metadata=metadata,
                ),
            )
            return result

        result = GenerateResult(
            text="Reached the maximum number of reasoning steps.",
            finish_reason=FinishReason.MAX_STEPS,
            metadata=last_metadata,
        )
        session.append_assistant_message(result.text, metadata=last_metadata)
        await self._emit_event(
            event_handler,
            RuntimeEvent(
                event_type=RuntimeEventType.ASSISTANT_MESSAGE,
                step_index=self.max_steps,
                text=result.text,
                metadata=last_metadata,
            ),
        )
        return result

    async def _execute_tool_batch(
        self,
        *,
        batch_id: str,
        step_index: int,
        tool_calls: list[ToolCall],
        context: AgentRuntimeContext,
        session: Session,
        event_handler: RuntimeEventHandler | None,
    ) -> list[ToolCallOutcome]:
        total_calls = len(tool_calls)
        for call_index, tool_call in enumerate(tool_calls):
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
                self._execute_tool_call_outcome(
                    batch_id=batch_id,
                    call_index=call_index,
                    total_calls=total_calls,
                    context=context,
                    session=session,
                    tool_call=tool_call,
                )
            )
            for call_index, tool_call in enumerate(tool_calls)
        ]
        outcomes: list[ToolCallOutcome] = []
        try:
            for completed_task in asyncio.as_completed(tasks):
                outcome = await completed_task
                outcomes.append(outcome)
                await self._emit_event(
                    event_handler,
                    RuntimeEvent(
                        event_type=(
                            RuntimeEventType.TOOL_CALL_FAILED
                            if outcome.result.is_error
                            else RuntimeEventType.TOOL_CALL_COMPLETED
                        ),
                        step_index=step_index,
                        batch_id=outcome.batch_id,
                        call_index=outcome.call_index,
                        total_calls=outcome.total_calls,
                        tool_call=outcome.tool_call,
                        tool_result=outcome.result,
                    ),
                )
        finally:
            for task in tasks:
                if task.done():
                    continue
                task.cancel()
        return outcomes

    async def _execute_tool_call_outcome(
        self,
        *,
        batch_id: str,
        call_index: int,
        total_calls: int,
        context: AgentRuntimeContext,
        session: Session,
        tool_call: ToolCall,
    ) -> ToolCallOutcome:
        result = await self._execute_tool_call(
            context=context,
            session=session,
            tool_call=tool_call,
        )
        return ToolCallOutcome(
            batch_id=batch_id,
            call_index=call_index,
            total_calls=total_calls,
            tool_call=tool_call,
            result=result,
        )

    async def _execute_tool_call(
        self,
        *,
        context: AgentRuntimeContext,
        session: Session,
        tool_call: ToolCall,
    ) -> ToolExecutionResult:
        tool = next(
            (candidate for candidate in context.tools if candidate.spec.name == tool_call.name),
            None,
        )
        if tool is None:
            return ToolExecutionResult(
                content=f"Tool '{tool_call.name}' is not available.",
                is_error=True,
            )

        exec_context = context.get_tool_execution_context(session.session_id)
        try:
            return await tool.execute(tool_call.arguments, exec_context)
        except Exception as exc:
            return ToolExecutionResult(
                content=f"Tool '{tool_call.name}' failed: {exc}",
                is_error=True,
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
