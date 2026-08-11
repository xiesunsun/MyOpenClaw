"""按纯状态机决定推进 AgentRun 的默认 Tool Loop。"""

from __future__ import annotations

import copy
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

from pickel.context.model_context_builder import ModelContextBuilder
from pickel.context.hook_feedback import HookFeedback, append_hook_feedback
from pickel.context.model_context import ModelContext
from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    agent_message_from_dict,
)
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.conversations.conversation_service import ConversationService
from pickel.operations.agent_run_state import AgentRunState, ToolCallState
from pickel.operations.operation_service import OperationService
from pickel.hooks.events import (
    BeforeRequestEvent,
    PostToolBatchEvent,
    PostToolUseEvent,
    PreToolUseEvent,
    AgentRunEndEvent,
)
from pickel.providers.stream import StreamDelta
from pickel.runtime.operation_state_machine import OperationStateMachine
from pickel.runtime.runtime_bindings import RuntimeBindings
from pickel.runtime.runtime_effects import RuntimeEffects
from pickel.runtime.host_calls import HostCallClient
from pickel.tools.base import ToolExecutionResult
from pickel.tools.validation import validate_tool_arguments

StreamDeltaConsumer = Callable[[StreamDelta], None | Awaitable[None]]


@dataclass(frozen=True)
class ModelStepStartedProgress:
    operation_id: str
    step_id: str
    step_sequence: int


@dataclass(frozen=True)
class ToolCallStartedProgress:
    operation_id: str
    step_id: str
    step_sequence: int
    tool_call: ToolCallState
    call_index: int
    total_calls: int


@dataclass(frozen=True)
class ToolCallCompletedProgress(ToolCallStartedProgress):
    result: ToolExecutionResult


OperationProgress = (
    ModelStepStartedProgress | ToolCallStartedProgress | ToolCallCompletedProgress
)
OperationProgressConsumer = Callable[
    [OperationProgress],
    None | Awaitable[None],
]


@dataclass(frozen=True)
class OperationDriveResult:
    operation_id: str
    status: str
    state: AgentRunState
    assistant_message: AssistantMessage | None = None


class OperationDriver:
    """协调状态、Context 和 Effects；不直接执行任何真实副作用。"""

    def __init__(
        self,
        *,
        bindings: RuntimeBindings,
        operation_service: OperationService,
        conversation_service: ConversationService,
        runtime_effects: RuntimeEffects,
        model_context_builder: ModelContextBuilder | None = None,
        state_machine: OperationStateMachine | None = None,
        step_id_factory: Callable[[], str] | None = None,
        node_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._bindings = bindings
        self._operation_service = operation_service
        self._conversation_service = conversation_service
        self._effects = runtime_effects
        self._context_builder = model_context_builder or ModelContextBuilder()
        self._state_machine = state_machine or OperationStateMachine()
        self._step_id_factory = step_id_factory or (lambda: str(uuid4()))
        self._node_id_factory = node_id_factory or (lambda: str(uuid4()))

    async def drive_operation(
        self,
        operation_id: str,
        *,
        consume_delta: StreamDeltaConsumer | None = None,
        consume_progress: OperationProgressConsumer | None = None,
        host_calls: HostCallClient | None = None,
    ) -> OperationDriveResult:
        """推进直到成功、终态或需要人工恢复的暂停点。"""
        operation = self._operation_service.load_session_operation(operation_id)
        state = self._operation_service.load_agent_run_state(operation_id)
        owned_model_intent_step_id: str | None = None
        owned_tool_intents: set[str] = set()

        # 进程重启后 Provider intent 可显式 retry；Tool intent 默认不可重放。
        if (
            state.current_step is not None
            and state.current_step.phase == "model_request_intent_recorded"
        ):
            state = self._commit(
                self._state_machine.schedule_model_request_retry(state)
            )
        if self._has_unknown_tool_effect(state):
            waiting = self._state_machine.pause_for_unknown_tool_effect(state)
            state = self._commit(waiting)
            return OperationDriveResult(operation_id, "waiting", state)

        while True:
            decision = self._state_machine.decide_next_action(state)
            if decision.action == "done":
                return OperationDriveResult(operation_id, state.status, state)
            if decision.action == "pause":
                return OperationDriveResult(operation_id, "waiting", state)
            if decision.action == "start_model_step":
                state = self._commit(
                    self._state_machine.start_model_step(
                        state,
                        step_id=self._step_id_factory(),
                    )
                )
                assert state.current_step is not None
                await self._notify_progress(
                    consume_progress,
                    ModelStepStartedProgress(
                        operation_id=state.operation_id,
                        step_id=state.current_step.step_id,
                        step_sequence=state.current_step.step_sequence,
                    ),
                )
                continue
            if decision.action == "record_model_request_intent":
                state = self._commit(
                    self._state_machine.record_model_request_intent(state)
                )
                assert state.current_step is not None
                owned_model_intent_step_id = state.current_step.step_id
                continue
            if decision.action == "execute_model_request":
                step = state.current_step
                assert step is not None
                if owned_model_intent_step_id != step.step_id:
                    state = self._commit(
                        self._state_machine.schedule_model_request_retry(state)
                    )
                    continue
                entries = self._conversation_service.list_active_branch_entries(
                    session_id=operation.session_id
                )
                context = self._context_builder.build_model_context(
                    agent_package_version=self._bindings.agent_package_version,
                    conversation_entries=entries,
                )
                recalled_messages = await self._effects.retrieve_recall_messages(
                    session_id=operation.session_id,
                    visible_messages=context.messages,
                )
                context = ModelContext(
                    system=context.system,
                    messages=append_hook_feedback(
                        [*context.messages, *recalled_messages],
                        [
                            HookFeedback(
                                source_event="PersistedHookFeedback",
                                text=text,
                            )
                            for text in state.model_context_feedback
                        ],
                    ),
                    tools=context.tools,
                )
                before = await self._effects.invoke_hook(
                    "before_request",
                    BeforeRequestEvent(
                        session_id=operation.session_id,
                        operation_id=operation.operation_id,
                        step_id=step.step_id,
                        step_sequence=step.step_sequence,
                        model_context=context,
                    ),
                )
                if before.model_context is not None:
                    context = before.model_context
                if before.feedback_text:
                    context = ModelContext(
                        system=context.system,
                        messages=append_hook_feedback(
                            context.messages,
                            [
                                HookFeedback(
                                    source_event="BeforeRequest",
                                    text=before.feedback_text,
                                )
                            ],
                        ),
                        tools=context.tools,
                    )
                result = await self._effects.execute_model_request(
                    state=state,
                    model_context=context,
                    consume_delta=consume_delta,
                )
                assistant_node_id = self._node_id_factory()
                next_state = self._state_machine.record_model_request_completed(
                    state,
                    assistant_message_node_id=assistant_node_id,
                )
                state = self._effects.commit_operation_state(
                    state=next_state,
                    appended_message=result.assistant_message,
                    appended_message_node_id=assistant_node_id,
                ).state
                owned_model_intent_step_id = None
                continue
            if decision.action == "prepare_tool_calls":
                assistant = self._load_current_assistant_message(
                    operation.session_id,
                    state,
                )
                tool_calls = []
                for block in assistant.content:
                    if not isinstance(block, ToolCallBlock):
                        continue
                    entry = self._bindings.tool_snapshot.get(block.name)
                    arguments = dict(block.arguments)
                    action = "allow"
                    reason = None
                    if entry is not None:
                        pre = await self._effects.invoke_hook(
                            "pre_tool_use",
                            PreToolUseEvent(
                                session_id=operation.session_id,
                                operation_id=operation.operation_id,
                                step_id=state.current_step.step_id,
                                step_sequence=state.current_step.step_sequence,
                                tool_name=block.name,
                                tool_call_id=block.id,
                                arguments=arguments,
                                tool_source=entry.source.value,
                                tool_origin=entry.origin,
                            ),
                        )
                        if pre.updated_arguments is not None:
                            arguments = dict(pre.updated_arguments)
                        action = pre.action
                        reason = pre.reason
                        invalid = validate_tool_arguments(entry.tool, arguments)
                        if invalid is not None:
                            action = "deny"
                            reason = "Hook 修改后的工具参数不符合 schema：" f"{invalid}"
                    tool_calls.append(
                        ToolCallState(
                            tool_call_id=block.id,
                            tool_name=block.name,
                            arguments=arguments,
                            execution_state="ready",
                            execution_policy=(
                                "deny"
                                if action == "deny"
                                else "confirm" if action == "ask" else "execute"
                            ),
                            decision_reason=reason,
                        )
                    )
                state = self._commit(
                    self._state_machine.prepare_tool_calls(
                        state,
                        tool_calls=tuple(tool_calls),
                    )
                )
                continue
            if decision.action == "record_tool_call_intent":
                assert decision.tool_call_id is not None
                state = self._commit(
                    self._state_machine.record_tool_call_intent(
                        state,
                        tool_call_id=decision.tool_call_id,
                    )
                )
                owned_tool_intents.add(decision.tool_call_id)
                continue
            if decision.action == "execute_tool_call":
                assert decision.tool_call_id is not None
                if decision.tool_call_id not in owned_tool_intents:
                    state = self._commit(
                        self._state_machine.pause_for_unknown_tool_effect(state)
                    )
                    return OperationDriveResult(operation_id, "waiting", state)
                progress_tool_call = self._find_tool_call(
                    state,
                    decision.tool_call_id,
                )
                assert state.current_step is not None
                call_index = next(
                    index
                    for index, call in enumerate(state.current_step.tool_calls)
                    if call.tool_call_id == decision.tool_call_id
                )
                await self._notify_progress(
                    consume_progress,
                    ToolCallStartedProgress(
                        operation_id=state.operation_id,
                        step_id=state.current_step.step_id,
                        step_sequence=state.current_step.step_sequence,
                        tool_call=progress_tool_call,
                        call_index=call_index,
                        total_calls=len(state.current_step.tool_calls),
                    ),
                )
                result = await self._effects.execute_tool_call(
                    state=state,
                    tool_call_id=decision.tool_call_id,
                    host_calls=host_calls,
                )
                tool_call = self._find_tool_call(state, decision.tool_call_id)
                entry = self._bindings.tool_snapshot.get(tool_call.tool_name)
                post = await self._effects.invoke_hook(
                    "post_tool_use",
                    PostToolUseEvent(
                        session_id=operation.session_id,
                        operation_id=operation.operation_id,
                        step_id=state.current_step.step_id,
                        step_sequence=state.current_step.step_sequence,
                        tool_name=tool_call.tool_name,
                        tool_call_id=tool_call.tool_call_id,
                        arguments=dict(tool_call.arguments),
                        result_content=result.content,
                        is_error=result.is_error,
                        tool_source=entry.source.value if entry is not None else "",
                        tool_origin=entry.origin if entry is not None else None,
                    ),
                )
                result_node_id = self._node_id_factory()
                next_state = self._state_machine.record_tool_call_completed(
                    state,
                    tool_call_id=decision.tool_call_id,
                    result_message_node_id=result_node_id,
                    is_error=result.is_error,
                    feedback_text=post.feedback_text,
                )
                state = self._effects.commit_operation_state(
                    state=next_state,
                    appended_message=self._build_tool_result_message(
                        tool_call,
                        result,
                    ),
                    appended_message_node_id=result_node_id,
                ).state
                await self._notify_progress(
                    consume_progress,
                    ToolCallCompletedProgress(
                        operation_id=state.operation_id,
                        step_id=next_state.current_step.step_id,
                        step_sequence=next_state.current_step.step_sequence,
                        tool_call=tool_call,
                        call_index=call_index,
                        total_calls=len(next_state.current_step.tool_calls),
                        result=copy.deepcopy(result),
                    ),
                )
                owned_tool_intents.remove(decision.tool_call_id)
                continue
            if decision.action == "invoke_post_tool_batch_hook":
                step = state.current_step
                assert step is not None
                outcomes = [
                    {
                        "tool_call_id": call.tool_call_id,
                        "tool_name": call.tool_name,
                        "is_error": call.is_error,
                    }
                    for call in step.tool_calls
                ]
                batch = await self._effects.invoke_hook(
                    "post_tool_batch",
                    PostToolBatchEvent(
                        session_id=operation.session_id,
                        operation_id=operation.operation_id,
                        step_id=step.step_id,
                        step_sequence=step.step_sequence,
                        outcomes=outcomes,
                    ),
                )
                state = self._commit(
                    self._state_machine.record_post_tool_batch_hook_completed(
                        state,
                        feedback_text=batch.feedback_text,
                    )
                )
                continue
            if decision.action == "complete_model_step":
                state = self._commit(self._state_machine.complete_model_step(state))
                continue
            if decision.action == "archive_model_step":
                step = state.current_step
                assert step is not None
                if (
                    step.step_sequence
                    >= self._bindings.agent_package_version.runtime.max_model_steps
                ):
                    message = AssistantMessage(
                        content=[
                            TextBlock(
                                text=("Reached the maximum number of reasoning steps.")
                            )
                        ]
                    )
                    node_id = self._node_id_factory()
                    next_state = self._state_machine.finish_agent_run(
                        state,
                        final_assistant_node_id=node_id,
                    )
                    state = self._effects.commit_operation_state(
                        state=next_state,
                        appended_message=message,
                        appended_message_node_id=node_id,
                    ).state
                    await self._invoke_agent_run_end(operation.session_id, state)
                    return OperationDriveResult(
                        operation_id,
                        "succeeded",
                        state,
                        message,
                    )
                state = self._commit(
                    self._state_machine.archive_completed_model_step(state)
                )
                continue
            if decision.action == "finish_agent_run":
                assistant = self._load_current_assistant_message(
                    operation.session_id,
                    state,
                )
                step = state.current_step
                assert step is not None
                assert step.assistant_message_node_id is not None
                state = self._commit(
                    self._state_machine.finish_agent_run(
                        state,
                        final_assistant_node_id=step.assistant_message_node_id,
                    )
                )
                await self._invoke_agent_run_end(operation.session_id, state)
                return OperationDriveResult(
                    operation_id,
                    "succeeded",
                    state,
                    assistant,
                )
            raise RuntimeError(f"未实现的 Operation action: {decision.action}")

    def _commit(self, state: AgentRunState) -> AgentRunState:
        return self._effects.commit_operation_state(state=state).state

    async def _invoke_agent_run_end(
        self,
        session_id: str,
        state: AgentRunState,
    ) -> None:
        await self._effects.invoke_hook(
            "agent_run_end",
            AgentRunEndEvent(
                session_id=session_id,
                operation_id=state.operation_id,
                step_id=(
                    state.current_step.step_id
                    if state.current_step is not None
                    else None
                ),
                step_sequence=(
                    state.current_step.step_sequence
                    if state.current_step is not None
                    else None
                ),
                reason=state.status,
            ),
        )

    @staticmethod
    async def _notify_progress(
        consumer: OperationProgressConsumer | None,
        progress: OperationProgress,
    ) -> None:
        if consumer is None:
            return
        result = consumer(progress)
        if hasattr(result, "__await__"):
            await result

    @staticmethod
    def _has_unknown_tool_effect(state: AgentRunState) -> bool:
        step = state.current_step
        return bool(
            step is not None
            and step.phase == "tool_calls_running"
            and any(
                call.execution_state == "intent_recorded" for call in step.tool_calls
            )
        )

    def _load_current_assistant_message(
        self,
        session_id: str,
        state: AgentRunState,
    ) -> AssistantMessage:
        step = state.current_step
        if step is None or step.assistant_message_node_id is None:
            raise RuntimeError("ModelStepState 没有 AssistantMessage 引用")
        for entry in reversed(
            self._conversation_service.list_active_branch_entries(session_id=session_id)
        ):
            if entry.node.node_id != step.assistant_message_node_id:
                continue
            message = agent_message_from_dict(entry.object.content)
            if not isinstance(message, AssistantMessage):
                raise RuntimeError("assistant_message_node_id 未指向 AssistantMessage")
            return message
        raise RuntimeError(
            f"AssistantMessage 节点不存在: {step.assistant_message_node_id}"
        )

    @staticmethod
    def _find_tool_call(
        state: AgentRunState,
        tool_call_id: str,
    ) -> ToolCallState:
        step = state.current_step
        if step is not None:
            for tool_call in step.tool_calls:
                if tool_call.tool_call_id == tool_call_id:
                    return tool_call
        raise RuntimeError(f"ToolCallState 不存在: {tool_call_id}")

    @staticmethod
    def _build_tool_result_message(
        tool_call: ToolCallState,
        result: ToolExecutionResult,
    ) -> ToolResultMessage:
        content = (
            copy.deepcopy(result.content_blocks)
            if result.content_blocks
            else [TextBlock(text=result.content)]
        )
        return ToolResultMessage(
            tool_call_id=tool_call.tool_call_id,
            tool_name=tool_call.tool_name,
            content=content,
            is_error=result.is_error,
            structured_content=copy.deepcopy(result.structured_content),
        )
