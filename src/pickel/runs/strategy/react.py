"""ReAct：ModelContext + 分 checkpoint 落盘。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
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
from pickel.conversations.session import Session
from pickel.runs.run import Run
from pickel.runs.estimator import request_char_count
from pickel.runs.event_bus import EventBus
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    RuntimeEventBase,
    StepStarted,
    ToolCallCompleted,
    ToolCallStarted,
)
from pickel.runs.turn_usage import last_turn_usage
from pickel.runs.usage_anchor import context_fingerprint
from pickel.runs.strategy.base import ExecutionStrategy
from pickel.runs.turn_state import TurnState
from pickel.shared.event_envelope import EventEnvelope
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
        bus: EventBus | None = None,
        turn_id: str | None = None,
        initial_hook_feedback: list[HookFeedback] | None = None,
    ) -> AssistantMessage:
        turn = TurnState() if turn_id is None else TurnState(turn_id=turn_id)

        def envelope(step_index: int | None = None) -> EventEnvelope:
            return EventEnvelope(
                session_id=session.session_id,
                turn_id=turn.turn_id,
                step_index=step_index,
            )

        if initial_hook_feedback:
            turn.step_hook_feedback.extend(initial_hook_feedback)
            turn.hook_feedback.extend(initial_hook_feedback)

        for step_index in range(1, self.max_steps + 1):
            step = turn.begin_step(step_index)
            await self._emit(bus, StepStarted(envelope=envelope(step_index)))

            model_context = await prepare(
                run=run,
                session=session,
                hook_feedback=turn.hook_feedback_for_current_step(),
                unit_window=run.unit_window,
                recall_sources=run.recall_sources,
            )
            # 指纹取 prepare 输出（hook 前）：/context 预览不跑 hook，
            # 记 hook 后的 Request 会让有 hook 时锚永远失效。
            prepared_fingerprint = context_fingerprint(
                model_context,
                provider=run.agent.model_config.provider,
                model=run.agent.model_config.model,
            )
            prepared_chars = request_char_count(model_context)
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
            assistant = self._ensure_metadata(
                run,
                assistant,
                elapsed_ms,
                context_fingerprint_value=prepared_fingerprint,
                hook_injected_chars=(
                    request_char_count(model_context) - prepared_chars
                ),
            )

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
                await self._emit(
                    bus,
                    AssistantMessageEvent(
                        envelope=envelope(step_index),
                        text=text,
                        usage=last_turn_usage(session),
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
                await self._emit(
                    bus,
                    ToolCallStarted(
                        envelope=envelope(step_index),
                        tool_call=self._event_tool_call(tool_call),
                        batch_id=batch_id,
                        call_index=call_index,
                        total_calls=len(tool_calls),
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
                await self._emit(
                    bus,
                    ToolCallCompleted(
                        envelope=envelope(step_index),
                        tool_call=self._event_tool_call(tool_call),
                        tool_result=replace(result, metadata=dict(result.metadata)),
                        batch_id=batch_id,
                        call_index=call_index,
                        total_calls=len(tool_calls),
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
        # 合成消息不带 metadata：它不是一次模型调用，复用最后一次 generate 的
        # metadata 会让 last_turn_usage / session_usage 把那次用量数第二遍
        # （AssistantMessageEvent 与 TurnCompleted 就会对同一 turn 给出不同数字）。
        # 无 metadata 的 assistant 由 resolve_anchor 当作可跳过的 trailing 消息，
        # 锚仍落在最后一次真实调用上——见 usage_anchor.resolve_anchor。
        max_msg = AssistantMessage(
            content=[
                TextContent(text="Reached the maximum number of reasoning steps.")
            ],
        )
        entry = session.append_assistant(max_msg)
        self._flush_entry(run, session, entry)
        await self._emit(
            bus,
            AssistantMessageEvent(
                envelope=envelope(self.max_steps),
                text="Reached the maximum number of reasoning steps.",
                usage=last_turn_usage(session),
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
    def _event_tool_call(tool_call: ToolCallContent) -> ToolCall:
        """事件用的 tool call 快照：arguments 必须是拷贝（红线 8）。

        共享同一个 dict 就等于把执行参数交给订阅者：emit 之后本方法下面才取
        `dict(tool_call.arguments)` 去执行、才构造 PreToolUse 事件，订阅者改一下
        就能同时劫持工具执行与 hook 输入。每次 emit 都建新拷贝，事件之间也不串。
        """
        return ToolCall(
            id=tool_call.id,
            name=tool_call.name,
            arguments=dict(tool_call.arguments),
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
        *,
        context_fingerprint_value: str,
        hook_injected_chars: int,
    ) -> AssistantMessage:
        """补齐可观测 metadata：耗时、上下文指纹、hook 改写量。

        指纹与 hook 改写量只有本层知道，provider 无从填写，故一律在此覆盖。
        """
        meta = assistant.metadata
        if meta is None:
            meta = ModelResponseMetadata(
                provider=run.agent.model_config.provider,
                model=run.agent.model_config.model,
            )
        return AssistantMessage(
            content=list(assistant.content),
            metadata=replace(
                meta,
                elapsed_ms=meta.elapsed_ms if meta.elapsed_ms is not None else elapsed_ms,
                context_fingerprint=context_fingerprint_value,
                hook_injected_chars=max(0, hook_injected_chars),
            ),
        )

    @staticmethod
    async def _emit(bus: EventBus | None, event: RuntimeEventBase) -> None:
        if bus is None:
            return
        await bus.emit(event)
