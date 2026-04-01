import inspect
import time

from myopenclaw.conversation.message import ToolCall
from myopenclaw.conversation.metadata import MessageMetadata
from myopenclaw.conversation.session import Session
from myopenclaw.runtime.events import RuntimeEvent, RuntimeEventType
from myopenclaw.runtime.generation import FinishReason, GenerateRequest, GenerateResult
from myopenclaw.runtime.context import AgentRuntimeContext
from myopenclaw.runtime.strategy.base import ExecutionStrategy, RuntimeEventHandler
from myopenclaw.tools.base import ToolExecutionResult


class ReActStrategy(ExecutionStrategy):
    """Reason+Act (ReAct) execution strategy."""

    def __init__(self, max_steps: int = 8) -> None:
        self.max_steps = max_steps

    async def execute(
        self,
        context: AgentRuntimeContext,
        session: Session,
        event_handler: RuntimeEventHandler | None = None,
    ) -> GenerateResult:
        last_metadata: MessageMetadata | None = None

        for step_index in range(1, self.max_steps + 1):
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
                    messages=list(session.messages),
                    tools=[tool.spec for tool in context.tools],
                )
            )
            elapsed_ms = round((time.perf_counter() - start) * 1000)
            metadata = result.metadata or MessageMetadata(
                provider=context.agent.model_config.provider,
                model=context.agent.model_config.model,
                input_tokens=result.usage.input_tokens if result.usage else None,
                output_tokens=result.usage.output_tokens if result.usage else None,
                elapsed_ms=elapsed_ms,
            )
            result.metadata = metadata
            last_metadata = metadata

            if result.tool_calls:
                session.append_assistant_message(
                    content=result.text,
                    metadata=metadata,
                    tool_calls=result.tool_calls,
                )
                for tool_call in result.tool_calls:
                    await self._emit_event(
                        event_handler,
                        RuntimeEvent(
                            event_type=RuntimeEventType.TOOL_CALL_STARTED,
                            step_index=step_index,
                            tool_call=tool_call,
                        ),
                    )
                    tool_result = await self._execute_tool_call(
                        context=context,
                        session=session,
                        tool_call=tool_call,
                    )
                    session.append_tool_result(
                        content=tool_result.content,
                        tool_call_id=tool_call.id,
                        tool_name=tool_call.name,
                        is_error=tool_result.is_error,
                        metadata=tool_result.metadata,
                    )
                    await self._emit_event(
                        event_handler,
                        RuntimeEvent(
                            event_type=RuntimeEventType.TOOL_CALL_COMPLETED,
                            step_index=step_index,
                            tool_call=tool_call,
                            tool_result=tool_result,
                        ),
                    )
                continue

            session.append_assistant_message(result.text, metadata=metadata)
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
