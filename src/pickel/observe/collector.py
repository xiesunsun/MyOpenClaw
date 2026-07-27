"""Session → SessionTrajectory 只读派生（设计 §4）。

真源是 Session entry；本模块不写任何存储、不发网络请求。
trace 增强经参数注入（TraceEnhancement 鸭子类型：tool_timings / turn_markers），
避免与 trace_reader 产生 import 依赖方向问题。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Protocol

from pickel.conversations.agent_message import (
    AgentMessage,
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
    agent_message_from_dict,
)
from pickel.conversations.content_blocks import (
    TextContent,
    ThinkingContent,
    ToolCallContent,
)
from pickel.conversations.repository import SessionRepository
from pickel.conversations.session import Session
from pickel.conversations.session_entry import (
    ENTRY_TYPE_COMPACTION,
    ENTRY_TYPE_MESSAGE,
)
from pickel.observe.model import SessionTrajectory, Step, ToolExecution, Turn

_USAGE_KEYS = ("input", "cache_read", "cache_write", "output", "actual_input")


class _Enhancement(Protocol):
    tool_timings: dict[str, Any]
    turn_markers: list[Any]


def collect_trajectory(
    session: Session,
    *,
    enhancement: _Enhancement | None = None,
    result_preview_chars: int = 2000,
) -> SessionTrajectory:
    turns: list[Turn] = []
    compaction_steps: list[int] = []
    step_count = 0

    current_steps: list[Step] = []
    current_query: str | None = None

    def flush_turn() -> None:
        nonlocal current_steps, current_query
        if current_query is None and not current_steps:
            return
        turns.append(_build_turn(len(turns), current_query or "", current_steps))
        current_steps = []
        current_query = None

    for entry in session.active_path():
        if entry.entry_type == ENTRY_TYPE_COMPACTION:
            compaction_steps.append(step_count)
            continue
        if entry.entry_type != ENTRY_TYPE_MESSAGE:
            continue
        message = _message_from_payload(entry.payload)
        if message is None:
            continue

        if isinstance(message, UserMessage):
            flush_turn()
            current_query = _text_of(message.content)
        elif isinstance(message, AssistantMessage):
            current_steps.append(
                _build_step(len(current_steps), message)
            )
            step_count += 1
        elif isinstance(message, ToolResultMessage):
            current_steps = _attach_result(
                current_steps, message, result_preview_chars
            )

    flush_turn()

    if enhancement is not None:
        turns = _apply_enhancement(turns, enhancement)

    session_usage = _sum_usages([turn.usage_totals for turn in turns])
    return SessionTrajectory(
        session_id=session.session_id,
        agent_id=session.agent_id,
        cwd=session.cwd,
        title=session.title,
        created_at=session.created_at.isoformat(),
        updated_at=session.updated_at.isoformat(),
        turns=turns,
        compaction_steps=compaction_steps,
        session_usage=session_usage,
        trace_available=enhancement is not None,
    )


def collect_previews(
    repository: SessionRepository, *, limit: int = 20
) -> list[Session]:
    """最近 limit 个含消息的会话（全量加载，供导出）。"""
    sessions: list[Session] = []
    for preview in repository.list(limit=limit):
        if preview.message_count <= 0:
            continue
        session = repository.load(preview.session_id)
        if session is not None:
            sessions.append(session)
    return sessions


def _message_from_payload(payload: dict[str, Any]) -> AgentMessage | None:
    try:
        return agent_message_from_dict(payload)
    except (KeyError, TypeError, ValueError):
        return None


def _text_of(blocks: list[Any]) -> str:
    return "".join(
        block.text for block in blocks if isinstance(block, TextContent)
    )


def _build_step(index: int, message: AssistantMessage) -> Step:
    metadata = message.metadata
    usage = metadata.usage if metadata else None
    input_tokens = (usage.input_tokens or 0) if usage else 0
    cache_read = (usage.cache_read_tokens or 0) if usage else 0
    cache_write = (usage.cache_write_tokens or 0) if usage else 0
    output_tokens = (usage.output_tokens or 0) if usage else 0
    return Step(
        index=index,
        thinking_chars=sum(
            len(block.text)
            for block in message.content
            if isinstance(block, ThinkingContent)
        ),
        text=_text_of(message.content),
        tool_executions=[
            ToolExecution(
                tool_call_id=block.id,
                name=block.name,
                arguments=dict(block.arguments),
            )
            for block in message.content
            if isinstance(block, ToolCallContent)
        ],
        model_label=(
            f"{metadata.provider} / {metadata.model}" if metadata else ""
        ),
        finish_reason=metadata.finish_reason if metadata else None,
        usage={
            "input": input_tokens,
            "cache_read": cache_read,
            "cache_write": cache_write,
            "output": output_tokens,
            "actual_input": input_tokens + cache_read + cache_write,
        },
        elapsed_ms=metadata.elapsed_ms if metadata else None,
        hook_injected_chars=metadata.hook_injected_chars if metadata else None,
        context_fingerprint=metadata.context_fingerprint if metadata else None,
    )


def _attach_result(
    steps: list[Step],
    message: ToolResultMessage,
    preview_chars: int,
) -> list[Step]:
    preview = _text_of(message.content)[:preview_chars]

    for step_index in range(len(steps) - 1, -1, -1):
        executions = steps[step_index].tool_executions
        for execution_index, execution in enumerate(executions):
            if (
                execution.tool_call_id == message.tool_call_id
                and not execution.orphan
                and execution.result_preview == ""
            ):
                updated = list(executions)
                updated[execution_index] = replace(
                    execution,
                    result_preview=preview,
                    is_error=message.is_error,
                )
                steps = list(steps)
                steps[step_index] = replace(
                    steps[step_index], tool_executions=updated
                )
                return steps

    # 孤儿结果：挂到最后一个 step；无 step 则造一个空 step 承载，不丢数据。
    orphan = ToolExecution(
        tool_call_id=message.tool_call_id,
        name=message.tool_name,
        arguments={},
        result_preview=preview,
        is_error=message.is_error,
        orphan=True,
    )
    steps = list(steps)
    if not steps:
        steps.append(
            Step(
                index=0,
                thinking_chars=0,
                text="",
                tool_executions=[],
                model_label="",
                finish_reason=None,
                usage={key: 0 for key in _USAGE_KEYS},
                elapsed_ms=None,
                hook_injected_chars=None,
                context_fingerprint=None,
            )
        )
    last = steps[-1]
    steps[-1] = replace(last, tool_executions=[*last.tool_executions, orphan])
    return steps


def _build_turn(index: int, query: str, steps: list[Step]) -> Turn:
    final_text = steps[-1].text if steps else ""
    return Turn(
        index=index,
        query=query,
        steps=steps,
        final_text=final_text,
        usage_totals=_sum_usages([step.usage for step in steps]),
        elapsed_ms=sum(step.elapsed_ms or 0 for step in steps),
    )


def _sum_usages(usages: list[dict[str, int]]) -> dict[str, int]:
    totals = {key: 0 for key in _USAGE_KEYS}
    for usage in usages:
        for key in _USAGE_KEYS:
            totals[key] += usage.get(key, 0)
    return totals


def _apply_enhancement(
    turns: list[Turn], enhancement: _Enhancement
) -> list[Turn]:
    timings = enhancement.tool_timings
    enhanced: list[Turn] = []
    markers = (
        enhancement.turn_markers
        if len(enhancement.turn_markers) == len(turns)
        else None
    )

    digest_groups = getattr(enhancement, "request_digests", None)
    if not (
        isinstance(digest_groups, list) and len(digest_groups) == len(turns)
    ):
        digest_groups = None

    for turn_index, turn in enumerate(turns):
        digests = (
            digest_groups[turn_index]
            if digest_groups is not None
            and len(digest_groups[turn_index]) == len(turn.steps)
            else None
        )
        steps = []
        for step in turn.steps:
            if digests is not None:
                step = replace(step, request_digest=digests[step.index])
            executions = [
                (
                    replace(
                        execution,
                        started_at=timing.started_at,
                        completed_at=timing.completed_at,
                        duration_ms=timing.duration_ms,
                    )
                    if (timing := timings.get(execution.tool_call_id))
                    else execution
                )
                for execution in step.tool_executions
            ]
            steps.append(replace(step, tool_executions=executions))
        turn = replace(turn, steps=steps)
        if markers is not None:
            marker = markers[turn_index]
            turn = replace(
                turn,
                started_at=marker.started_at,
                failed=marker.failed,
                interrupted=marker.interrupted,
            )
        enhanced.append(turn)
    return enhanced
