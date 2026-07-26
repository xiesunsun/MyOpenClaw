"""ReAct：ModelContext + 分 checkpoint 落盘。"""

from __future__ import annotations

import asyncio
import inspect
import time
from numbers import Real
from uuid import uuid4

from pickel.context.assembler import append_hook_feedback
from pickel.context.hook_feedback import HookFeedback
from pickel.context.model_context import ModelContext
from pickel.context.prepare import prepare
from pickel.hooks.events import (
    BeforeRequestEvent,
    PostToolBatchEvent,
    PostToolUseEvent,
    PreToolUseEvent,
    TurnEndEvent,
)
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ToolResultMessage,
)
from pickel.conversations.content_blocks import TextContent, ToolCallContent
from pickel.conversations.message import ToolCall
from pickel.conversations.metadata import MessageMetadata
from pickel.conversations.session import Session
from pickel.runs.run import Run
from pickel.runs.events import RuntimeEvent, RuntimeEventType
from pickel.runs.strategy.base import ExecutionStrategy, RuntimeEventHandler
from pickel.runs.turn_state import ToolExecutionOutcome, TurnState
from pickel.tools.base import ToolExecutionResult


class ReActStrategy(ExecutionStrategy):
    """Reason+Act：工具副作用前先落盘 assistant intent。"""

    DEFAULT_PROVIDER_TIMEOUT_SECONDS = 600.0

    def __init__(self, max_steps: int = 8) -> None:
        self.max_steps = max_steps

    async def execute(
        self,
        run: Run,
        session: Session,
        event_handler: RuntimeEventHandler | None = None,
        initial_hook_feedback: list[HookFeedback] | None = None,
    ) -> AssistantMessage:
        turn = TurnState()
        if initial_hook_feedback:
            turn.step_hook_feedback.extend(initial_hook_feedback)
            turn.hook_feedback.extend(initial_hook_feedback)
        last_assistant: AssistantMessage | None = None

        for step_index in range(1, self.max_steps + 1):
            step = turn.begin_step(step_index)
            await self._emit_event(
                event_handler,
                RuntimeEvent(
                    event_type=RuntimeEventType.MODEL_STEP_STARTED,
                    step_index=step_index,
                ),
            )

            model_context = prepare(
                run=run,
                session=session,
                hook_feedback=turn.hook_feedback_for_current_step(),
                unit_window=run.unit_window,
            )
            # before_request：可替换 context；feedback 并入当前请求消息
            before = await run.lifecycle_hooks.before_request(
                BeforeRequestEvent(
                    session_id=session.session_id,
                    turn_id=turn.turn_id,
                    step_index=step_index,
                    model_context=model_context,
                )
            )
            if before.model_context is not None:
                model_context = before.model_context
            if before.feedback_text:
                model_context = ModelContext(
                    system=model_context.system,
                    messages=append_hook_feedback(
                        model_context.messages,
                        [
                            HookFeedback(
                                source_event="BeforeRequest",
                                text=before.feedback_text,
                            )
                        ],
                    ),
                    tools=model_context.tools,
                )

            start = time.perf_counter()
            assistant = await self._generate_with_optional_timeout(
                run=run,
                context=model_context,
            )
            elapsed_ms = round((time.perf_counter() - start) * 1000)
            assistant = self._ensure_metadata(run, assistant, elapsed_ms)
            last_assistant = assistant

            # checkpoint BEFORE tools
            entry = session.append_assistant(assistant)
            step.assistant_entry_id = entry.entry_id
            self._flush_entry(run, session, entry)

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
                await run.lifecycle_hooks.turn_end(
                    TurnEndEvent(
                        session_id=session.session_id,
                        turn_id=turn.turn_id,
                        reason="completed",
                    )
                )
                return assistant

            step.pending_tool_call_ids = [call.id for call in tool_calls]
            batch_id = uuid4().hex
            batch_outcomes: list[dict] = []
            # 串行按调用顺序：PreToolUse → 执行或合成 → append_tool_result → PostToolUse
            for call_index, tool_call in enumerate(tool_calls):
                runtime_call = ToolCall(
                    id=tool_call.id,
                    name=tool_call.name,
                    arguments=tool_call.arguments,
                )
                await self._emit_event(
                    event_handler,
                    RuntimeEvent(
                        event_type=RuntimeEventType.TOOL_CALL_STARTED,
                        step_index=step_index,
                        batch_id=batch_id,
                        call_index=call_index,
                        total_calls=len(tool_calls),
                        tool_call=runtime_call,
                    ),
                )
                pre = await run.lifecycle_hooks.pre_tool_use(
                    PreToolUseEvent(
                        session_id=session.session_id,
                        turn_id=turn.turn_id,
                        step_index=step_index,
                        tool_name=tool_call.name,
                        tool_call_id=tool_call.id,
                        arguments=dict(tool_call.arguments),
                    )
                )
                if pre.action == "deny":
                    reason = pre.reason or "工具调用被 Hook 拒绝"
                    result = ToolExecutionResult(content=reason, is_error=True)
                else:
                    args = (
                        dict(pre.updated_arguments)
                        if pre.updated_arguments is not None
                        else dict(tool_call.arguments)
                    )
                    # 用可能更新后的参数执行
                    exec_call = ToolCallContent(
                        id=tool_call.id,
                        name=tool_call.name,
                        arguments=args,
                        thought_signature=tool_call.thought_signature,
                    )
                    result = await self._execute_tool_call(
                        run=run, session=session, tool_call=exec_call
                    )
                result_entry = session.append_tool_result(
                    ToolResultMessage(
                        tool_call_id=tool_call.id,
                        tool_name=tool_call.name,
                        content=[TextContent(text=result.content)],
                        is_error=result.is_error,
                    )
                )
                self._flush_entry(run, session, result_entry)
                step.completed_tool_call_ids.append(tool_call.id)
                batch_outcomes.append(
                    {
                        "tool_call_id": tool_call.id,
                        "tool_name": tool_call.name,
                        "is_error": result.is_error,
                        "content": result.content,
                    }
                )
                await self._emit_event(
                    event_handler,
                    RuntimeEvent(
                        event_type=(
                            RuntimeEventType.TOOL_CALL_FAILED
                            if result.is_error
                            else RuntimeEventType.TOOL_CALL_COMPLETED
                        ),
                        step_index=step_index,
                        batch_id=batch_id,
                        call_index=call_index,
                        total_calls=len(tool_calls),
                        tool_call=runtime_call,
                        tool_result=result,
                    ),
                )
                post = await run.lifecycle_hooks.post_tool_use(
                    PostToolUseEvent(
                        session_id=session.session_id,
                        turn_id=turn.turn_id,
                        step_index=step_index,
                        tool_name=tool_call.name,
                        tool_call_id=tool_call.id,
                        arguments=dict(tool_call.arguments),
                        result_content=result.content,
                        is_error=result.is_error,
                    )
                )
                if post.feedback_text:
                    fb = HookFeedback(
                        source_event="PostToolUse", text=post.feedback_text
                    )
                    turn.step_hook_feedback.append(fb)
                    turn.hook_feedback.append(fb)

            batch_decision = await run.lifecycle_hooks.post_tool_batch(
                PostToolBatchEvent(
                    session_id=session.session_id,
                    turn_id=turn.turn_id,
                    step_index=step_index,
                    outcomes=batch_outcomes,
                )
            )
            if batch_decision.feedback_text:
                fb = HookFeedback(
                    source_event="PostToolBatch", text=batch_decision.feedback_text
                )
                turn.step_hook_feedback.append(fb)
                turn.hook_feedback.append(fb)

        # max steps
        max_msg = AssistantMessage(
            content=[
                TextContent(text="Reached the maximum number of reasoning steps.")
            ],
            metadata=last_assistant.metadata if last_assistant else None,
        )
        entry = session.append_assistant(max_msg)
        self._flush_entry(run, session, entry)
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

    def _flush_entry(self, run: Run, session: Session, entry) -> None:
        if run.session_service is None:
            return
        run.session_service.flush_new_entries(session=session, entries=[entry])

    async def _generate_with_optional_timeout(self, *, run: Run, context):
        timeout_seconds = self._provider_timeout_seconds(run)
        if timeout_seconds is None:
            return await run.provider.generate(context)
        return await asyncio.wait_for(
            run.provider.generate(context),
            timeout=timeout_seconds,
        )

    @staticmethod
    def _provider_timeout_seconds(run: Run) -> float | None:
        timeout_seconds = run.agent.model_config.provider_options.get(
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
        run: Run,
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
                    run=run,
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
        run: Run,
        session: Session,
    ) -> ToolExecutionOutcome:
        result = await self._execute_tool_call(run=run, session=session, tool_call=tool_call)
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
        run: Run,
        session: Session,
        tool_call: ToolCallContent,
    ) -> ToolExecutionResult:
        tool = next(
            (candidate for candidate in run.tools if candidate.spec.name == tool_call.name),
            None,
        )
        if tool is None:
            return ToolExecutionResult(
                content=f"Tool '{tool_call.name}' is not available.",
                is_error=True,
            )
        exec_context = run.get_tool_execution_context(session.session_id)
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
        run: Run,
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
                provider=run.agent.model_config.provider,
                model=run.agent.model_config.model,
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
