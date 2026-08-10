"""ReAct：ModelContext + 分 checkpoint 落盘。"""

from __future__ import annotations

import asyncio
import copy
import time
from contextlib import aclosing, nullcontext
from dataclasses import replace
from numbers import Real
from uuid import uuid4

from pickel.context.hook_feedback import HookFeedback, append_hook_feedback
from pickel.context.model_context import ModelContext
from pickel.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ToolResultMessage,
)
from pickel.conversations.content_blocks import (
    TextContent,
    ToolCallContent,
    content_blocks_to_list,
)
from pickel.conversations.session import Session
from pickel.hooks.events import (
    BeforeRequestEvent,
    PostToolBatchEvent,
    PostToolUseEvent,
    PreToolUseEvent,
)
from pickel.observe.records import (
    ErrorInfo,
    ObservationIdentity,
    RequestSnapshotRecord,
    SpanTimer,
    observation_requested,
    record_request_snapshot,
)
from pickel.providers.stream import (
    StreamCompleted,
    TextDelta,
    ThinkingDelta,
    ToolCallArgsDelta,
)
from pickel.runs.estimator import request_char_count
from pickel.runs.event_bus import EventBus
from pickel.runs.host_call_types import (
    CONFIRMATION_CALL,
    ConfirmationAnswer,
    ConfirmationRequest,
    HostCallSource,
)
from pickel.runs.host_calls import (
    HostCallClient,
    HostCallCompleted,
    HostCallContext,
)
from pickel.runs.run import Run
from pickel.runs.runtime_events import (
    AssistantMessageEvent,
    RequestDigestEvent,
    RuntimeEventBase,
    StepStarted,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallArgsDeltaEvent,
    ToolCallCompleted,
    ToolCallStarted,
    TurnInterrupted,
)
from pickel.runs.strategy.base import ExecutionStrategy
from pickel.runs.tool_call import ToolCall
from pickel.runs.tool_result import build_tool_result_message
from pickel.runs.turn_mailbox import TurnInputReader
from pickel.runs.turn_state import TurnState
from pickel.runs.turn_usage import last_turn_usage
from pickel.runs.usage_anchor import context_fingerprint
from pickel.shared.event_envelope import EventEnvelope
from pickel.tools.base import ToolExecutionResult
from pickel.tools.bus import ToolEntry, ToolSnapshot, ToolSource
from pickel.tools.validation import validate_tool_arguments, validate_tool_result


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
        host_calls: HostCallClient | None = None,
        turn_input: TurnInputReader | None = None,
    ) -> AssistantMessage:
        turn = TurnState() if turn_id is None else TurnState(turn_id=turn_id)
        # turn 边界快照：整个 turn 的所有 step 共用同一份工具集。
        # 每 step 重取会让 prompt cache 每 step 失效，也可能让模型看到的定义
        # 与执行时找到的工具对象不一致。
        turn.tool_snapshot = run.tool_bus.snapshot(run.activation)

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

            identity = ObservationIdentity(
                session_id=session.session_id,
                turn_id=turn.turn_id,
                step_index=step_index,
            )
            step_timer = SpanTimer("pickel.step", identity)
            context_build_timer = SpanTimer("pickel.model_context.build", identity)
            try:
                model_context = await run.model_context_builder.build_model_context(
                    run=run,
                    session=session,
                    hook_feedback=turn.hook_feedback_for_current_step(),
                    unit_window=run.unit_window,
                    recall_sources=run.recall_sources,
                    tool_snapshot=turn.tool_snapshot,
                )
            except Exception as exc:
                context_build_timer.finish(
                    status="error",
                    error=ErrorInfo.from_exception(exc, kind="context"),
                )
                raise
            context_build_timer.finish(
                attributes={
                    "message_count": len(model_context.messages),
                    "tool_count": len(model_context.tools),
                }
            )
            # 指纹取 ModelContextBuilder 输出（hook 前）：/context 预览不跑 hook，
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

            final_request_chars = request_char_count(model_context)
            await self._emit(
                bus,
                RequestDigestEvent(
                    envelope=envelope(step_index),
                    system_sections=[
                        {"name": section.name, "chars": len(section.text)}
                        for section in model_context.system.sections
                    ],
                    tool_names=[tool.name for tool in model_context.tools],
                    message_count=len(model_context.messages),
                    request_chars=final_request_chars,
                    hook_injected_chars=final_request_chars - prepared_chars,
                ),
            )

            # 完整正文只在 full trace 下构造；standard 保持低开销摘要。
            if observation_requested("request_snapshot"):
                try:
                    snapshot = run.provider.request_snapshot(model_context)
                except Exception:
                    # 可观测快照是派生数据，Provider 的观测实现不能阻断请求。
                    snapshot = None
                if snapshot is not None:
                    record_request_snapshot(
                        RequestSnapshotRecord(
                            identity=identity,
                            provider=str(snapshot.get("provider") or ""),
                            model=str(snapshot.get("model") or ""),
                            cache_order=tuple(snapshot.get("cache_order") or ()),
                            request=dict(snapshot.get("request") or {}),
                        )
                    )

            start = time.perf_counter()
            provider_timer = SpanTimer(
                "pickel.provider.request",
                identity,
                attributes={
                    "provider": run.agent.model_config.provider,
                    "model": run.agent.model_config.model,
                },
            )
            try:
                assistant, ttft_ms = await self._generate_streaming(
                    run=run,
                    context=model_context,
                    bus=bus,
                    envelope=envelope,
                    step_index=step_index,
                )
            except asyncio.CancelledError:
                provider_timer.finish(status="cancelled")
                raise
            except Exception as exc:
                kind, retryable, status_code = self._provider_error_details(exc)
                provider_timer.finish(
                    status="error",
                    attributes={
                        "error_category": kind,
                        "status_code": status_code,
                        "retryable": retryable,
                    },
                    error=ErrorInfo.from_exception(exc, kind=kind, retryable=retryable),
                )
                raise
            elapsed_ms = round((time.perf_counter() - start) * 1000)
            assistant = self._ensure_metadata(
                run,
                assistant,
                elapsed_ms,
                context_fingerprint_value=prepared_fingerprint,
                hook_injected_chars=final_request_chars - prepared_chars,
            )
            metadata = assistant.metadata
            usage = metadata.usage if metadata else None
            provider_timer.finish(
                attributes={
                    "ttft_ms": ttft_ms,
                    "input_tokens": usage.input_tokens if usage else None,
                    "output_tokens": usage.output_tokens if usage else None,
                    "cache_read_tokens": usage.cache_read_tokens if usage else None,
                    "cache_write_tokens": usage.cache_write_tokens if usage else None,
                }
            )

            # checkpoint BEFORE tools
            entry = session.append_assistant(assistant)
            step.assistant_entry_id = entry.entry_id
            self._flush_entry(run, session, entry, identity=identity)

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
                steered = await self._deliver_steering(
                    run=run,
                    session=session,
                    bus=bus,
                    turn=turn,
                    turn_input=turn_input,
                    finishing=True,
                )
                if steered:
                    step_timer.finish(
                        attributes={"tool_call_count": 0, "steered": True}
                    )
                    continue
                turn.final_assistant_entry_id = entry.entry_id
                turn.status = "completed"
                step_timer.finish(attributes={"tool_call_count": 0})
                return assistant

            step.pending_tool_call_ids = [call.id for call in tool_calls]
            batch_id = uuid4().hex
            batch_outcomes: list[dict] = []
            # 串行按调用顺序：PreToolUse → 执行或合成 → append_tool_result → PostToolUse
            try:
                for call_index, tool_call in enumerate(tool_calls):
                    entry = (
                        turn.tool_snapshot.get(tool_call.name)
                        if turn.tool_snapshot is not None
                        else None
                    )
                    result: ToolExecutionResult | None = None
                    pre = None
                    validation_stage = "passed"
                    args = dict(tool_call.arguments)
                    if entry is None:
                        validation_stage = "tool_not_found"
                        result = self._validation_failure(
                            f"Tool '{tool_call.name}' is not available.",
                            error_type="ToolNotAvailable",
                        )
                    else:
                        invalid = validate_tool_arguments(entry.tool, args)
                        if invalid is not None:
                            validation_stage = "arguments"
                            result = self._validation_failure(
                                f"工具参数不符合 schema：{invalid}",
                                error_type="ToolArgumentsInvalid",
                            )
                        else:
                            pre = await run.lifecycle_hooks.pre_tool_use(
                                PreToolUseEvent(
                                    session_id=session.session_id,
                                    turn_id=turn.turn_id,
                                    step_index=step_index,
                                    tool_name=tool_call.name,
                                    tool_call_id=tool_call.id,
                                    arguments=dict(args),
                                    tool_source=entry.source.value,
                                    tool_origin=entry.origin,
                                )
                            )
                            if pre.updated_arguments is not None:
                                args = dict(pre.updated_arguments)
                            invalid = validate_tool_arguments(entry.tool, args)
                            if invalid is not None:
                                validation_stage = "hook_arguments"
                                result = self._validation_failure(
                                    f"Hook 修改后的工具参数不符合 schema：{invalid}",
                                    error_type="ToolArgumentsInvalid",
                                )
                    effective_call = ToolCallContent(
                        id=tool_call.id,
                        name=tool_call.name,
                        arguments=args,
                        thought_signature=tool_call.thought_signature,
                    )
                    confirmation = (
                        "pending"
                        if result is None and pre is not None and pre.action == "ask"
                        else "not_requested"
                    )
                    await self._emit(
                        bus,
                        ToolCallStarted(
                            envelope=envelope(step_index),
                            tool_call=self._event_tool_call(effective_call),
                            batch_id=batch_id,
                            call_index=call_index,
                            total_calls=len(tool_calls),
                            tool_source=entry.source.value if entry else None,
                            tool_origin=entry.origin if entry else None,
                            validation=validation_stage,
                            hook_action=pre.action if pre else None,
                            confirmation=confirmation,
                        ),
                    )
                    tool_timer = SpanTimer(
                        "pickel.tool.execute",
                        identity,
                        attributes={
                            "tool_name": tool_call.name,
                            "tool_call_id": tool_call.id,
                            "tool_source": entry.source.value if entry else None,
                            "tool_origin": entry.origin if entry else None,
                            "validation": validation_stage,
                            "hook_action": pre.action if pre else None,
                        },
                    )
                    denied_reason: str | None = None
                    denied_type = "ToolDenied"
                    if result is None and pre is not None and pre.action == "deny":
                        denied_reason = pre.reason or "工具调用被 Hook 拒绝"
                    elif result is None and pre is not None and pre.action == "ask":
                        assert entry is not None
                        accepted = await self._confirm_tool_call(
                            host_calls=host_calls,
                            entry=entry,
                            tool_call=effective_call,
                            reason=pre.reason,
                            session_id=session.session_id,
                            turn_id=turn.turn_id,
                            step_index=step_index,
                        )
                        if accepted:
                            confirmation = "accepted"
                        else:
                            confirmation = "declined_or_unavailable"
                            denied_type = "ToolConfirmationDeclined"
                            denied_reason = "工具调用未获得用户确认"
                            if pre.reason:
                                denied_reason += f"：{pre.reason}"

                    if result is not None:
                        tool_timer.finish(status="error", error=result.error)
                    elif denied_reason is not None:
                        result = ToolExecutionResult(
                            content=denied_reason,
                            is_error=True,
                            error=ErrorInfo(
                                kind="denied",
                                type=denied_type,
                                message=denied_reason,
                                retryable=False,
                            ),
                        )
                        tool_timer.finish(
                            status="denied",
                            attributes={"confirmation": confirmation},
                            error=result.error,
                        )
                    else:
                        try:
                            result = await self._execute_tool_call(
                                run=run,
                                session=session,
                                tool_call=effective_call,
                                snapshot=turn.tool_snapshot,
                                turn_id=turn.turn_id,
                                step_index=step_index,
                                host_calls=host_calls,
                            )
                        except asyncio.CancelledError:
                            tool_timer.finish(status="cancelled")
                            raise
                        tool_timer.finish(
                            status="error" if result.is_error else "ok",
                            attributes={"confirmation": confirmation},
                            error=result.error,
                        )
                    result_message = build_tool_result_message(effective_call, result)
                    result_entry = session.append_tool_result(result_message)
                    self._flush_entry(run, session, result_entry, identity=identity)
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
                            tool_call=self._event_tool_call(effective_call),
                            tool_result=copy.deepcopy(result),
                            tool_result_message=copy.deepcopy(result_message),
                            batch_id=batch_id,
                            call_index=call_index,
                            total_calls=len(tool_calls),
                            tool_source=entry.source.value if entry else None,
                            tool_origin=entry.origin if entry else None,
                            validation=validation_stage,
                            hook_action=pre.action if pre else None,
                            confirmation=confirmation,
                        ),
                    )
                    post = await run.lifecycle_hooks.post_tool_use(
                        PostToolUseEvent(
                            session_id=session.session_id,
                            turn_id=turn.turn_id,
                            step_index=step_index,
                            tool_name=tool_call.name,
                            tool_call_id=tool_call.id,
                            arguments=dict(effective_call.arguments),
                            result_content=result.content,
                            is_error=result.is_error,
                            tool_source=entry.source.value if entry else "",
                            tool_origin=entry.origin if entry else None,
                        )
                    )
                    if post.feedback_text:
                        fb = HookFeedback(
                            source_event="PostToolUse", text=post.feedback_text
                        )
                        turn.step_hook_feedback.append(fb)
                        turn.hook_feedback.append(fb)
            except asyncio.CancelledError:
                # 中断时补齐未完成的 tool_result：session 里留下悬空的
                # tool_call 会让下一轮请求被 provider 直接拒绝
                # （Anthropic 与 Gemini 都要求 tool_use 与 tool_result 配对）。
                self._complete_pending_tool_calls(
                    run=run,
                    session=session,
                    step=step,
                    tool_calls=tool_calls,
                )
                await self._emit(
                    bus,
                    TurnInterrupted(
                        envelope=envelope(step_index),
                        at_step=step_index,
                        partial_text=self._assistant_text(assistant),
                    ),
                )
                # CancelledError 继承 BaseException，吞掉会破坏 asyncio 取消机制
                raise

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
            await self._deliver_steering(
                run=run,
                session=session,
                bus=bus,
                turn=turn,
                turn_input=turn_input,
                finishing=False,
            )
            step_timer.finish(attributes={"tool_call_count": len(tool_calls)})

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
        self._flush_entry(run, session, entry, identity=identity)
        await self._emit(
            bus,
            AssistantMessageEvent(
                envelope=envelope(self.max_steps),
                text="Reached the maximum number of reasoning steps.",
                usage=last_turn_usage(session),
            ),
        )
        return max_msg

    async def _deliver_steering(
        self,
        *,
        run: Run,
        session: Session,
        bus: EventBus | None,
        turn: TurnState,
        turn_input: TurnInputReader | None,
        finishing: bool,
    ) -> bool:
        """在工具批后或结束前交付至多一条有效 steering。"""
        if turn_input is None:
            return False

        take = (
            turn_input.finish_or_take_steering
            if finishing
            else turn_input.take_steering
        )
        while True:
            item = await take()
            if item is None:
                return False
            try:
                accepted, feedback, _reason = await run.deliver_user_prompt(
                    session=session,
                    user_message=item.message,
                    bus=bus,
                    turn_id=turn.turn_id,
                    source="steer",
                    pending_input=item,
                )
            except BaseException:
                await turn_input.put_back(item)
                raise
            if not accepted:
                continue
            turn.step_hook_feedback.extend(feedback)
            turn.hook_feedback.extend(feedback)
            return True

    def _complete_pending_tool_calls(
        self,
        *,
        run: Run,
        session: Session,
        step,
        tool_calls: list[ToolCallContent],
    ) -> None:
        """给已落盘但未完成的 tool_call 补一条中断标记的 tool_result。"""
        completed = set(step.completed_tool_call_ids)
        for tool_call in tool_calls:
            if tool_call.id in completed:
                continue
            entry = session.append_tool_result(
                ToolResultMessage(
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.name,
                    content=[TextContent(text="工具执行被用户中断")],
                    is_error=True,
                )
            )
            self._flush_entry(run, session, entry)

    def _flush_entry(
        self,
        run: Run,
        session: Session,
        entry,
        *,
        identity: ObservationIdentity | None = None,
    ) -> None:
        if run.session_service is None:
            return
        timer = SpanTimer(
            "pickel.session.append",
            identity or ObservationIdentity(session_id=session.session_id),
            attributes={"entry_type": entry.entry_type, "entry_count": 1},
        )
        try:
            run.session_service.flush_new_entries(session=session, entries=[entry])
        except Exception as exc:
            timer.finish(
                status="error",
                error=ErrorInfo.from_exception(exc, kind="storage"),
            )
            raise
        timer.finish()

    async def _generate_streaming(
        self,
        *,
        run: Run,
        context,
        bus: EventBus | None,
        envelope,
        step_index: int,
    ) -> tuple[AssistantMessage, float | None]:
        """消费 provider.stream，把增量转成事件，返回最终消息。

        超时语义与改造前一致：包住整个消费过程的总时长，
        不是两次 delta 之间的间隔。
        """
        timeout_seconds = self._provider_timeout_seconds(run)
        text_parts: list[str] = []
        started = time.perf_counter()
        first_chunk_ms: list[float | None] = [None]
        coro = self._consume_stream(
            run=run,
            context=context,
            bus=bus,
            envelope=envelope,
            step_index=step_index,
            text_parts=text_parts,
            started=started,
            first_chunk_ms=first_chunk_ms,
        )
        try:
            if timeout_seconds is None:
                assistant = await coro
            else:
                assistant = await asyncio.wait_for(coro, timeout=timeout_seconds)
            return assistant, first_chunk_ms[0]
        except asyncio.CancelledError:
            # 用户中断落在 stream 消费期：工具循环那处 except 只覆盖
            # 工具执行期，这里不发 TurnInterrupted 的话 UI 收不到任何
            # 中断事件。两处互斥不会双发——stream 期取消到不了工具循环，
            # 工具期取消时本 try 早已正常返回。
            # 必须在 wait_for 外层捕获：超时是 wait_for 取消内层 coro，
            # 在 _consume_stream 里捕获会把超时误报成用户中断；这里
            # 超时抛 TimeoutError，不进本分支，行为保持现状。
            await self._emit(
                bus,
                TurnInterrupted(
                    envelope=envelope(step_index),
                    at_step=step_index,
                    partial_text="".join(text_parts),
                ),
            )
            # CancelledError 继承 BaseException，吞掉会破坏 asyncio 取消机制
            raise

    async def _consume_stream(
        self,
        *,
        run: Run,
        context,
        bus: EventBus | None,
        envelope,
        step_index: int,
        text_parts: list[str],
        started: float,
        first_chunk_ms: list[float | None],
    ) -> AssistantMessage:
        """取到 StreamCompleted 就返回；上游生成器显式关闭。

        text_parts 由调用方持有，逐个收集已流出的 TextDelta 文本：
        取消把本协程整个撕掉，调用方要在 except 里拿到已生成的
        partial_text，只能靠这种外部可见的累积。

        `async for` 里 return/break 不会关闭上游 async generator——它的
        finally 要等 GC 或事件循环 shutdown 才跑，上游握着 provider 的
        HTTP 流时这就是连接泄漏。与 providers.stream.accumulate 同一做法：
        对带 aclose() 的迭代器用 aclosing 显式收尾（超时取消时同样收尾），
        纯 AsyncIterator 没有要关的资源，直接消费。
        """
        iterator = run.provider.stream(context).__aiter__()
        closer = aclosing(iterator) if hasattr(iterator, "aclose") else nullcontext()
        async with closer:
            async for delta in iterator:
                if first_chunk_ms[0] is None:
                    first_chunk_ms[0] = round((time.perf_counter() - started) * 1000, 3)
                if isinstance(delta, StreamCompleted):
                    return delta.message
                if isinstance(delta, TextDelta):
                    text_parts.append(delta.text)
                event = self._delta_to_event(delta, envelope(step_index))
                if event is not None:
                    await self._emit(bus, event)
        raise ValueError("provider.stream 未以 StreamCompleted 收尾")

    @staticmethod
    def _delta_to_event(delta, envelope_value):
        if isinstance(delta, TextDelta):
            return TextDeltaEvent(envelope=envelope_value, text=delta.text)
        if isinstance(delta, ThinkingDelta):
            return ThinkingDeltaEvent(envelope=envelope_value, text=delta.text)
        if isinstance(delta, ToolCallArgsDelta):
            return ToolCallArgsDeltaEvent(
                envelope=envelope_value,
                tool_call_id=delta.tool_call_id,
                partial_json=delta.partial_json,
            )
        return None

    @staticmethod
    def _provider_timeout_seconds(run: Run) -> float | None:
        timeout_seconds = run.agent.model_config.provider_options.get("timeout_seconds")
        if timeout_seconds is None:
            return ReActStrategy.DEFAULT_PROVIDER_TIMEOUT_SECONDS
        if not isinstance(timeout_seconds, Real):
            return ReActStrategy.DEFAULT_PROVIDER_TIMEOUT_SECONDS
        timeout_value = float(timeout_seconds)
        if timeout_value <= 0:
            return ReActStrategy.DEFAULT_PROVIDER_TIMEOUT_SECONDS
        return timeout_value

    @staticmethod
    def _provider_error_details(
        exc: Exception,
    ) -> tuple[str, bool | None, int | None]:
        status = getattr(exc, "status_code", None)
        if not isinstance(status, int):
            response = getattr(exc, "response", None)
            candidate = getattr(response, "status_code", None)
            status = candidate if isinstance(candidate, int) else None
        name = type(exc).__name__.lower()
        if isinstance(exc, TimeoutError) or "timeout" in name:
            return "timeout", True, status
        if status == 429 or "ratelimit" in name or "rate_limit" in name:
            return "rate_limit", True, status
        if status in {401, 403} or "authentication" in name:
            return "authentication", False, status
        if status is not None and 400 <= status < 500:
            return "bad_request", False, status
        if status is not None and status >= 500:
            return "unavailable", True, status
        return "unknown", None, status

    async def _execute_tool_call(
        self,
        *,
        run: Run,
        session: Session,
        tool_call: ToolCallContent,
        snapshot: ToolSnapshot | None,
        turn_id: str,
        step_index: int,
        host_calls: HostCallClient | None,
    ) -> ToolExecutionResult:
        # 按 ToolEntry.name 查找：模型看到的名字（含命名空间前缀）就是查找键
        entry = snapshot.get(tool_call.name) if snapshot is not None else None
        if entry is None:
            return ToolExecutionResult(
                content=f"Tool '{tool_call.name}' is not available.",
                is_error=True,
                error=ErrorInfo(
                    kind="validation",
                    type="ToolNotAvailable",
                    message=f"Tool '{tool_call.name}' is not available.",
                    retryable=False,
                ),
            )
        exec_context = run.get_tool_execution_context(
            session.session_id,
            operation_id=turn_id,
            step_id=f"{turn_id}:{step_index}",
            step_sequence=step_index,
            tool_call_id=tool_call.id,
            host_calls=host_calls,
        )
        try:
            result = await entry.tool.execute(tool_call.arguments, exec_context)
            invalid = validate_tool_result(entry.tool, result)
            if invalid is not None:
                return self._validation_failure(
                    f"工具结果不符合 output_schema：{invalid}",
                    error_type="ToolResultInvalid",
                    metadata={
                        "invalid_result": {
                            "content": result.content,
                            "content_blocks": content_blocks_to_list(
                                result.content_blocks
                            ),
                            "structured_content": result.structured_content,
                            "is_error": result.is_error,
                            "metadata": copy.deepcopy(result.metadata),
                        }
                    },
                )
            return result
        except Exception as exc:  # noqa: BLE001 — 工具失败转错误结果
            return ToolExecutionResult(
                content=f"Tool '{tool_call.name}' failed: {exc}",
                is_error=True,
                error=ErrorInfo.from_exception(exc, kind="exception"),
            )

    @staticmethod
    def _validation_failure(
        message: str,
        *,
        error_type: str,
        metadata: dict | None = None,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(
            content=message,
            is_error=True,
            metadata=dict(metadata or {}),
            error=ErrorInfo(
                kind="validation",
                type=error_type,
                message=message,
                retryable=False,
            ),
        )

    @staticmethod
    async def _confirm_tool_call(
        *,
        host_calls: HostCallClient | None,
        entry: ToolEntry,
        tool_call: ToolCallContent,
        reason: str | None,
        session_id: str,
        turn_id: str,
        step_index: int,
    ) -> bool:
        if host_calls is None or not host_calls.supports(CONFIRMATION_CALL):
            return False
        source_kind = "mcp" if entry.source is ToolSource.MCP else "tool"
        outcome = await host_calls.call(
            CONFIRMATION_CALL,
            ConfirmationRequest(
                source=HostCallSource(
                    kind=source_kind,
                    name=entry.origin or entry.name,
                    operation=entry.name,
                ),
                title=f"确认执行工具 {entry.name}",
                message=reason or f"工具 {entry.name} 请求执行",
            ),
            HostCallContext(
                session_id=session_id,
                operation_id=turn_id,
                step_id=f"{turn_id}:{step_index}",
                step_sequence=step_index,
                tool_call_id=tool_call.id,
            ),
        )
        return (
            isinstance(outcome, HostCallCompleted)
            and isinstance(outcome.value, ConfirmationAnswer)
            and outcome.value.decision == "accept"
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
                elapsed_ms=(
                    meta.elapsed_ms if meta.elapsed_ms is not None else elapsed_ms
                ),
                context_fingerprint=context_fingerprint_value,
                hook_injected_chars=max(0, hook_injected_chars),
            ),
        )

    @staticmethod
    async def _emit(bus: EventBus | None, event: RuntimeEventBase) -> None:
        if bus is None:
            return
        await bus.emit(event)
