"""OperationDriver：推进一个已接受的 SessionOperation。

接受 Inbox、恢复 active Operation 属于 AgentDriver；本类只消费一个已有
Operation，并严格执行 ``intent commit -> 外部副作用 -> 结果 commit``。
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import uuid4

from pickel.agents.agent_package import AgentPackageVersion
from pickel.agents.agent_package_loader import PackageLoadError
from pickel.context.model_context import ModelContext
from pickel.context.model_context_builder import (
    ContextContributions,
    ModelContextBuilder,
)
from pickel.context.projection import ConversationProjector
from pickel.context.window import apply_window
from pickel.conversations.agent_message import (
    AgentMessage,
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.conversations.conversation_service import ConversationService
from pickel.conversations.conversation_node import ConversationNode
from pickel.hooks.decisions import PreToolUseDecision
from pickel.hooks.events import BeforeRequestEvent, PreToolUseEvent
from pickel.operations.agent_run_state import (
    AgentRunError,
    AgentRunState,
    ModelRequestIntent,
    ModelStepState,
    ToolApproval,
    ToolCallState,
    ToolReplayPolicy,
)
from pickel.operations.session_operation import SessionOperation
from pickel.operations.operation_service import OperationService
from pickel.providers.stream import StreamDelta
from pickel.runtime.agent_run_state_machine import AgentRunStateMachine
from pickel.runtime.runtime_effects import RuntimeEffects
from pickel.shared.frozen_json import freeze_json_object
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.tools.base import ToolExecutionResult
from pickel.tools.validation import validate_json_schema

StreamDeltaConsumer = Callable[[StreamDelta], None | Awaitable[None]]


@dataclass(frozen=True)
class OperationDriveResult:
    operation_id: str
    status: str
    state: AgentRunState
    assistant_message: AssistantMessage | None = None


class OperationDriver:
    """唯一的 Agent Tool Loop；不接受新消息，不拥有 Store。"""

    def __init__(
        self,
        *,
        operation_service: OperationService,
        conversation_service: ConversationService,
        package_loader: Callable[[str], AgentPackageVersion],
        effects_resolver: Callable[[str], RuntimeEffects],
        model_context_builder: ModelContextBuilder | None = None,
        state_machine: AgentRunStateMachine | None = None,
        step_id_factory: Callable[[], str] | None = None,
        node_id_factory: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._operations = operation_service
        self._conversations = conversation_service
        self._package_loader = package_loader
        self._effects_resolver = effects_resolver
        self._context_builder = model_context_builder or ModelContextBuilder()
        self._state_machine = state_machine or AgentRunStateMachine()
        self._step_id = step_id_factory or (lambda: str(uuid4()))
        self._node_id = node_id_factory or (lambda: str(uuid4()))
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def drive_operation(
        self,
        operation_id: str,
        *,
        consume_delta: StreamDeltaConsumer | None = None,
        host_calls=None,
    ) -> OperationDriveResult:
        operation = self._operations.load_operation(operation_id)
        state = self._operations.load_agent_run_state(operation_id)
        if state.status in {"succeeded", "failed", "cancelled"}:
            return OperationDriveResult(operation_id, state.status, state, None)
        try:
            package = self._package_loader(operation.agent_package_version_id)
            effects = self._effects_resolver(operation.agent_package_version_id)
        except PackageLoadError as exc:
            failed = replace(
                state,
                status="failed",
                current_step=None,
                error=AgentRunError(
                    code=exc.code,
                    message=str(exc),
                    retryable=True,
                ),
            )
            state = self._commit(failed, state)
            return OperationDriveResult(operation_id, "failed", state, None)
        if package.package_version_id != operation.agent_package_version_id:
            raise RuntimeError("Package Loader 返回了错误的 AgentPackageVersion")
        last_assistant: AssistantMessage | None = None

        while True:
            if state.status in {"succeeded", "failed", "cancelled"}:
                return OperationDriveResult(
                    operation_id, state.status, state, last_assistant
                )
            if state.status == "waiting":
                return OperationDriveResult(
                    operation_id, "waiting", state, last_assistant
                )

            if state.status == "cancelling":
                step = state.current_step
                if step is not None and any(
                    call.status == "intent_recorded" and call.replay_policy == "never"
                    for call in step.tool_calls
                ):
                    return OperationDriveResult(
                        operation_id, "cancelling", state, last_assistant
                    )
                state = self._commit(
                    replace(state, status="cancelled", current_step=None), state
                )
                return OperationDriveResult(
                    operation_id, "cancelled", state, last_assistant
                )

            if state.status == "queued":
                state = self._commit(replace(state, status="running"), state)
                continue

            step = state.current_step
            if step is None:
                if state.completed_step_count >= package.runtime_policy.max_model_steps:
                    state = self._commit(
                        replace(
                            state,
                            status="failed",
                            error=AgentRunError(
                                code="max_model_steps_exceeded",
                                message="AgentRun 已达到最大模型 Step 数",
                                retryable=False,
                            ),
                        ),
                        state,
                    )
                    return OperationDriveResult(
                        operation_id, "failed", state, last_assistant
                    )
                step = ModelStepState(
                    step_id=self._step_id(),
                    step_sequence=state.completed_step_count + 1,
                    phase="preparing_request",
                    request_attempt=0,
                    request_intent=None,
                    assistant_message_node_id=None,
                    tool_calls=(),
                )
                state = self._commit(
                    replace(state, current_step=step, status="running"), state
                )
                continue

            if step.phase == "preparing_request":
                context = await self._build_context(
                    operation=operation,
                    state=state,
                    package=package,
                    effects=effects,
                )
                intent = ModelRequestIntent(
                    model_context=context,
                    context_fingerprint=_fingerprint(context),
                )
                next_step = replace(
                    step,
                    phase="request_ready",
                    request_intent=intent,
                )
                state = self._commit(replace(state, current_step=next_step), state)
                continue

            if step.phase == "request_ready":
                # 只读取持久化 intent；恢复时不重新执行 Context/Recall/Hook。
                assert step.request_intent is not None
                state = self._commit(
                    replace(
                        state,
                        current_step=replace(
                            step, request_attempt=step.request_attempt + 1
                        ),
                    ),
                    state,
                )
                step = state.current_step
                assert step is not None and step.request_intent is not None
                result = await effects.execute_model_request(
                    operation=operation,
                    state=state,
                    model_context=step.request_intent.model_context,
                    consume_delta=consume_delta,
                    context_fingerprint=step.request_intent.context_fingerprint,
                )
                assistant_node_id = self._node_id()
                tool_calls = await self._prepare_tool_calls(
                    result.assistant_message,
                    operation=operation,
                    state=state,
                    package=package,
                    effects=effects,
                )
                if tool_calls:
                    has_waiting_approval = any(
                        call.status == "waiting_approval" for call in tool_calls
                    )
                    next_step = replace(
                        step,
                        phase="awaiting_tools",
                        request_intent=None,
                        assistant_message_node_id=assistant_node_id,
                        tool_calls=tool_calls,
                    )
                    state = self._commit(
                        replace(
                            state,
                            status="waiting" if has_waiting_approval else "running",
                            waiting_reason=(
                                "tool_approval" if has_waiting_approval else None
                            ),
                            current_step=next_step,
                        ),
                        state,
                        message=result.assistant_message,
                        node_id=assistant_node_id,
                    )
                    last_assistant = result.assistant_message
                    continue

                next_state = replace(
                    state,
                    status="succeeded",
                    current_step=None,
                    completed_step_count=state.completed_step_count + 1,
                    final_assistant_node_id=assistant_node_id,
                )
                state = self._commit(
                    next_state,
                    state,
                    message=result.assistant_message,
                    node_id=assistant_node_id,
                )
                last_assistant = result.assistant_message
                return OperationDriveResult(
                    operation_id, "succeeded", state, last_assistant
                )

            if step.phase != "awaiting_tools":
                raise RuntimeError(f"未知 ModelStep phase: {step.phase}")

            pending = next(
                (call for call in step.tool_calls if call.status != "completed"), None
            )
            if pending is not None and pending.status == "rejected":
                result_node_id = self._node_id()
                completed = replace(
                    pending,
                    status="completed",
                    result_node_id=result_node_id,
                    is_error=True,
                )
                calls = tuple(
                    (completed if call.tool_call_id == pending.tool_call_id else call)
                    for call in step.tool_calls
                )
                decision = pending.approval.decision if pending.approval else None
                reason = (
                    decision.reason if decision is not None else pending.decision_reason
                )
                content = "工具调用被拒绝"
                if reason:
                    content = f"{content}：{reason}"
                state = self._commit(
                    replace(
                        state,
                        current_step=replace(step, tool_calls=calls),
                    ),
                    state,
                    message=_tool_result_message(
                        pending,
                        ToolExecutionResult(content=content, is_error=True),
                    ),
                    node_id=result_node_id,
                )
                continue

            ready = (
                pending if pending is not None and pending.status == "ready" else None
            )
            executable: ToolCallState | None = None
            if ready is not None:
                # 保留 Provider 原始顺序，不能用只含当前调用的列表覆盖批次。
                next_calls = tuple(
                    (
                        replace(call, status="intent_recorded")
                        if call.tool_call_id == ready.tool_call_id
                        else call
                    )
                    for call in step.tool_calls
                )
                state = self._commit(
                    replace(state, current_step=replace(step, tool_calls=next_calls)),
                    state,
                )
                assert state.current_step is not None
                executable = next(
                    call
                    for call in state.current_step.tool_calls
                    if call.tool_call_id == ready.tool_call_id
                )
            else:
                recorded = (
                    pending
                    if pending is not None and pending.status == "intent_recorded"
                    else None
                )
                if recorded is not None and recorded.replay_policy == "never":
                    state = self._commit(
                        replace(
                            state,
                            status="waiting",
                            waiting_reason="tool_reconciliation",
                        ),
                        state,
                    )
                    return OperationDriveResult(
                        operation_id, "waiting", state, last_assistant
                    )
                executable = recorded

            if executable is not None:
                result = await effects.execute_tool_call(
                    operation=operation,
                    state=state,
                    tool_call_id=executable.tool_call_id,
                    host_calls=host_calls,
                )
                result_node_id = self._node_id()
                completed = replace(
                    next(
                        call
                        for call in state.current_step.tool_calls
                        if call.tool_call_id == executable.tool_call_id
                    ),
                    status="completed",
                    result_node_id=result_node_id,
                    is_error=result.is_error,
                )
                calls = tuple(
                    (
                        completed
                        if call.tool_call_id == executable.tool_call_id
                        else call
                    )
                    for call in state.current_step.tool_calls
                )
                next_step = replace(state.current_step, tool_calls=calls)
                message = _tool_result_message(executable, result)
                state = self._commit(
                    replace(state, current_step=next_step),
                    state,
                    message=message,
                    node_id=result_node_id,
                )
                continue

            if all(call.status == "completed" for call in step.tool_calls):
                state = self._commit(
                    replace(
                        state,
                        current_step=None,
                        completed_step_count=state.completed_step_count + 1,
                        status="running",
                        waiting_reason=None,
                    ),
                    state,
                )
                continue
            raise RuntimeError("ToolCallState 存在无法推进的状态")

    async def _prepare_tool_calls(
        self,
        message: AssistantMessage,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        package: AgentPackageVersion,
        effects: RuntimeEffects,
    ) -> tuple[ToolCallState, ...]:
        """在 AssistantMessage 提交前冻结 PreToolUse 的最终决定。"""
        step = state.current_step
        if step is None or step.phase != "request_ready":
            raise RuntimeError("PreToolUse 必须发生在 request_ready ModelStep")
        tools = {tool.name: tool for tool in package.tools}
        calls: list[ToolCallState] = []
        for block in message.content:
            if not isinstance(block, ToolCallBlock):
                continue
            tool = tools.get(block.name)
            if tool is None:
                calls.append(
                    _rejected_tool_call(
                        block,
                        arguments=dict(block.arguments),
                        reason=f"工具不可用: {block.name}",
                    )
                )
                continue

            decision = await effects.invoke_hook(
                "pre_tool_use",
                PreToolUseEvent(
                    identity=ExecutionIdentity(
                        session_id=operation.session_id,
                        operation_id=operation.operation_id,
                        step_id=step.step_id,
                        step_sequence=step.step_sequence,
                        tool_call_id=block.id,
                    ),
                    tool_name=block.name,
                    arguments=dict(block.arguments),
                    tool_source=tool.source.value,
                    tool_origin=tool.implementation_ref.name,
                ),
            )
            if not isinstance(decision, PreToolUseDecision):
                decision = PreToolUseDecision()
            try:
                arguments = (
                    dict(decision.updated_arguments)
                    if decision.updated_arguments is not None
                    else dict(block.arguments)
                )
                freeze_json_object(arguments)
            except (TypeError, ValueError):
                calls.append(
                    _rejected_tool_call(
                        block,
                        arguments=dict(block.arguments),
                        reason="PreToolUse Hook 返回了无效参数",
                    )
                )
                continue

            if decision.action == "deny":
                calls.append(
                    _rejected_tool_call(
                        block,
                        arguments=arguments,
                        reason=decision.reason or "工具调用被 Hook 拒绝",
                        replay_policy=tool.replay_policy,
                    )
                )
                continue
            validation_error = validate_json_schema(arguments, tool.input_schema)
            if validation_error is not None:
                calls.append(
                    _rejected_tool_call(
                        block,
                        arguments=arguments,
                        reason=f"工具参数无效: {validation_error}",
                        replay_policy=tool.replay_policy,
                    )
                )
                continue
            if decision.action == "ask":
                calls.append(
                    ToolCallState(
                        tool_call_id=block.id,
                        tool_name=block.name,
                        arguments=arguments,
                        status="waiting_approval",
                        approval=ToolApproval(
                            requested_at=self._now(),
                            requested_by="hook",
                            reason=decision.reason,
                            decision=None,
                        ),
                        replay_policy=tool.replay_policy,
                        execution_intent=None,
                        decision_reason=None,
                        result_node_id=None,
                        is_error=None,
                    )
                )
                continue
            calls.append(
                ToolCallState(
                    tool_call_id=block.id,
                    tool_name=block.name,
                    arguments=arguments,
                    status="ready",
                    approval=None,
                    replay_policy=tool.replay_policy,
                    execution_intent=None,
                    decision_reason=None,
                    result_node_id=None,
                    is_error=None,
                )
            )
        return tuple(calls)

    async def _build_context(
        self,
        *,
        operation: SessionOperation,
        state: AgentRunState,
        package: AgentPackageVersion,
        effects: RuntimeEffects,
    ) -> ModelContext:
        step = state.current_step
        if step is None or step.phase != "preparing_request":
            raise RuntimeError("Context 只能为 preparing_request Step 构建")
        nodes = self._conversations.list_active_branch_nodes(
            session_id=operation.session_id
        )
        projected = ConversationProjector().project_conversation_messages(nodes)
        visible = tuple(
            apply_window(
                projected,
                turn_window=package.runtime_policy.context_turn_window,
            )
        )
        recalled = await effects.retrieve_recall_messages(
            session_id=operation.session_id,
            visible_messages=visible,
        )
        hook_contributions = await effects.invoke_hook(
            "before_request",
            BeforeRequestEvent(
                identity=ExecutionIdentity(
                    session_id=operation.session_id,
                    operation_id=operation.operation_id,
                    step_id=step.step_id,
                    step_sequence=step.step_sequence,
                ),
                visible_messages=visible,
                recall_messages=tuple(recalled),
            ),
        )
        if not isinstance(hook_contributions, ContextContributions):
            hook_contributions = ContextContributions()
        return self._context_builder.build_model_context(
            package=package,
            visible_messages=visible,
            contributions=ContextContributions(
                system_sections=hook_contributions.system_sections,
                messages=tuple(recalled) + hook_contributions.messages,
            ),
        )

    def _commit(
        self,
        state: AgentRunState,
        previous: AgentRunState,
        *,
        message: AgentMessage | None = None,
        node_id: str | None = None,
    ) -> AgentRunState:
        next_state = replace(state, revision=previous.revision + 1)
        node = None
        if message is not None:
            if node_id is None:
                raise ValueError("提交 AgentMessage 时必须提供 node_id")
            operation = self._operations.load_operation(next_state.operation_id)
            session = self._conversations.load_conversation_session(
                operation.session_id
            )
            node = ConversationNode(
                node_id=node_id,
                session_id=operation.session_id,
                parent_node_id=session.active_node_id,
                content_type="agent_message",
                content=message,
                created_at=self._now(),
            )
        if not self._operations.commit_transition(
            state=next_state,
            expected_revision=previous.revision,
            node=node,
        ):
            raise RuntimeError("AgentRunState CAS 冲突，停止推进且不重放副作用")
        return next_state


def _fingerprint(context: ModelContext) -> str:
    return hashlib.sha256(context.to_json().encode("utf-8")).hexdigest()


def _rejected_tool_call(
    block: ToolCallBlock,
    *,
    arguments: dict[str, object],
    reason: str,
    replay_policy: ToolReplayPolicy = "never",
) -> ToolCallState:
    return ToolCallState(
        tool_call_id=block.id,
        tool_name=block.name,
        arguments=arguments,
        status="rejected",
        approval=None,
        replay_policy=replay_policy,
        execution_intent=None,
        decision_reason=reason,
        result_node_id=None,
        is_error=None,
    )


def _tool_result_message(
    call: ToolCallState, result: ToolExecutionResult
) -> ToolResultMessage:
    content = tuple(result.content_blocks) or (TextBlock(text=result.content),)
    return ToolResultMessage(
        tool_call_id=call.tool_call_id,
        tool_name=call.tool_name,
        content=content,
        is_error=result.is_error,
        structured_content=result.structured_content,
    )
