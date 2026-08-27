"""将可靠事实、ModelCall 不可变内容与 Trace 投影为展示数据合同。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from typing import Any, Mapping

from pickel.conversations.agent_message import agent_message_to_dict
from pickel.conversations.content_blocks import ToolCallBlock
from pickel.model_calls.content import ResponseContent
from pickel.model_calls.model_call import ModelCall
from pickel.observe.model_call_content_reader import (
    ModelCallContentReader,
    RequestContentReadResult,
    ResponseContentReadResult,
)
from pickel.observe.operation_fact_reader import (
    OperationFactReader,
    OperationFacts,
    OperationToolFact,
)
from pickel.observe.trace_reader import OperationTraceData


@dataclass(frozen=True)
class ModelCallTimingObservation:
    created_at: str
    started_at: str | None
    first_chunk_at: str | None
    finished_at: str | None
    latency_ms: float | None
    ttft_ms: float | None


@dataclass(frozen=True)
class ModelCallUsageObservation:
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    total_tokens: int | None
    cache_hit_rate: float | None
    cache_hit_rate_formula: str | None
    cache_hit_rate_denominator: int | None
    cache_hit_rate_source: str | None
    provider_reported: dict[str, Any] | None


@dataclass(frozen=True)
class ModelCallObservationItem:
    key: str
    model_call_id: str
    session_id: str
    operation_id: str | None
    step_id: str | None
    step_sequence: int | None
    attempt: int
    model_role: str
    purpose: str
    provider: str
    api_kind: str
    endpoint: str
    requested_model: str
    returned_model: str | None
    status: str
    context_fingerprint: str | None
    provider_request_id: str | None
    http_status: int | None
    request_content_ref: str
    response_content_ref: str | None
    request_content: RequestContentReadResult
    response_content: ResponseContentReadResult
    timing: ModelCallTimingObservation
    usage: ModelCallUsageObservation
    finish_reason: str | None
    error: dict[str, Any] | None


@dataclass(frozen=True)
class ExecutionTreeNode:
    key: str
    kind: str
    label: str
    meta: str
    status: str
    depth: int


@dataclass(frozen=True)
class ToolCallObservationItem:
    """从 Conversation Tree 投影的可靠工具调用。"""

    tool_call_id: str
    name: str
    arguments: dict[str, Any]
    result: dict[str, Any] | None
    is_error: bool | None
    step_id: str | None
    step_sequence: int | None
    source: str
    reliability: str

    @classmethod
    def from_fact(cls, fact: OperationToolFact) -> "ToolCallObservationItem":
        return cls(
            tool_call_id=fact.tool_call_id,
            name=fact.name,
            arguments=fact.arguments,
            result=fact.result,
            is_error=fact.is_error,
            step_id=fact.step_id,
            step_sequence=fact.step_sequence,
            source=fact.source,
            reliability=fact.reliability,
        )

    def to_dict(self) -> dict[str, Any]:
        return _json_serializable(
            {
                "tool_call_id": self.tool_call_id,
                "name": self.name,
                "arguments": self.arguments,
                "result": self.result,
                "is_error": self.is_error,
                "step_id": self.step_id,
                "step_sequence": self.step_sequence,
                "source": self.source,
                "reliability": self.reliability,
            }
        )


@dataclass(frozen=True)
class TimelineBar:
    key: str
    kind: str
    label: str
    duration_text: str
    status: str
    left_pct: float
    width_pct: float
    is_trace: bool = False


@dataclass(frozen=True)
class TimelineLane:
    name: str
    bars: list[TimelineBar] = field(default_factory=list)


@dataclass(frozen=True)
class TimelineData:
    start_time_iso: str
    end_time_iso: str
    total_duration_ms: float
    axis_ticks: list[str]
    lanes: list[TimelineLane]
    critical_path_text: str


def _json_serializable(val: Any) -> Any:
    if isinstance(val, Mapping):
        return {str(k): _json_serializable(v) for k, v in val.items()}
    if isinstance(val, (list, tuple)):
        return [_json_serializable(x) for x in val]
    return val


def _empty_usage() -> ModelCallUsageObservation:
    return ModelCallUsageObservation(
        input_tokens=None,
        output_tokens=None,
        cache_read_tokens=None,
        cache_write_tokens=None,
        reasoning_tokens=None,
        total_tokens=None,
        cache_hit_rate=None,
        cache_hit_rate_formula=None,
        cache_hit_rate_denominator=None,
        cache_hit_rate_source=None,
        provider_reported=None,
    )


def _cache_rate(
    *,
    provider: str,
    api_kind: str,
    input_tokens: int | None,
    cache_read_tokens: int | None,
    cache_write_tokens: int | None,
) -> tuple[float | None, int | None, str | None]:
    """按 Provider 的 usage 语义计算命中率；不确定时不制造百分比。"""

    if input_tokens is None or cache_read_tokens is None:
        return None, None, None
    normalized_api = api_kind.lower().replace("_", "-")
    normalized_provider = provider.lower()
    if normalized_api == "anthropic-messages" or normalized_provider == "anthropic":
        # Anthropic input_tokens 不包含 cache read/write。
        denominator = input_tokens + cache_read_tokens + (cache_write_tokens or 0)
        formula = "cache_read_tokens / (input_tokens + cache_read_tokens + cache_write_tokens)"
    elif normalized_api in {"openai-chat-completions", "openai-responses"} or (
        normalized_provider == "openai"
    ):
        # OpenAI 的 prompt/input_tokens 已包含缓存输入，cached_tokens 是其子集。
        denominator = input_tokens
        formula = "cache_read_tokens / input_tokens"
    else:
        return None, None, None
    if denominator <= 0:
        return None, denominator, formula
    return round(cache_read_tokens / denominator * 100, 2), denominator, formula


def _model_call_sort_key(call: ModelCall) -> tuple[datetime, str]:
    return call.created_at, call.model_call_id


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _timeline_status(status: Any) -> str:
    if status in {"succeeded", "completed", "ok"}:
        return "ok"
    if status in {"failed", "cancelled", "error"}:
        return "error"
    if status in {"incomplete", "affected"}:
        return "affected"
    return "unknown"


@dataclass(frozen=True)
class OperationObservationDocument:
    """唯一的 Provider-neutral 只读展示数据合同。"""

    session: dict[str, Any]
    operation: dict[str, Any]
    summary: dict[str, Any]
    model_calls: list[ModelCallObservationItem]
    execution_nodes: list[ExecutionTreeNode]
    timeline: TimelineData
    charts: dict[str, Any]
    document_evidence: dict[str, Any]
    trace_integrity: str
    tool_calls: list[ToolCallObservationItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _json_serializable(
            {
                "schema_version": 1,
                "scope": "operation",
                "session": self.session,
                "operation": self.operation,
                "summary": self.summary,
                "model_calls": [
                    {
                        "key": item.key,
                        "model_call_id": item.model_call_id,
                        "session_id": item.session_id,
                        "operation_id": item.operation_id,
                        "step_id": item.step_id,
                        "step_sequence": item.step_sequence,
                        "attempt": item.attempt,
                        "model_role": item.model_role,
                        "purpose": item.purpose,
                        "provider": item.provider,
                        "api_kind": item.api_kind,
                        "endpoint": item.endpoint,
                        "requested_model": item.requested_model,
                        "returned_model": item.returned_model,
                        "status": item.status,
                        "context_fingerprint": item.context_fingerprint,
                        "provider_request_id": item.provider_request_id,
                        "http_status": item.http_status,
                        "request_content_ref": item.request_content_ref,
                        "response_content_ref": item.response_content_ref,
                        "timing": asdict(item.timing),
                        "usage": asdict(item.usage),
                        "finish_reason": item.finish_reason,
                        "error": item.error,
                        "request_content_ok": item.request_content.is_ok,
                        "request_content_error": item.request_content.error,
                        "response_content_ok": item.response_content.is_ok,
                        "response_content_error": item.response_content.error,
                    }
                    for item in self.model_calls
                ],
                "execution_nodes": [asdict(node) for node in self.execution_nodes],
                "timeline": {
                    "start_time_iso": self.timeline.start_time_iso,
                    "end_time_iso": self.timeline.end_time_iso,
                    "total_duration_ms": self.timeline.total_duration_ms,
                    "axis_ticks": self.timeline.axis_ticks,
                    "lanes": [
                        {
                            "name": lane.name,
                            "bars": [asdict(b) for b in lane.bars],
                        }
                        for lane in self.timeline.lanes
                    ],
                    "critical_path_text": self.timeline.critical_path_text,
                },
                "charts": _json_serializable(self.charts),
                "document_evidence": _json_serializable(self.document_evidence),
                "trace_integrity": self.trace_integrity,
                "tool_calls": [item.to_dict() for item in self.tool_calls],
            }
        )

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            indent=2,
            default=str,
        )


def project_charts(model_call_items: list[ModelCallObservationItem]) -> dict[str, Any]:
    """只根据 ModelCall 观测构造图表序列，不读取 Store 或修改状态。"""

    latency_series = []
    cache_series = []
    token_series = []

    for item in model_call_items:
        latency_series.append(
            {
                "key": item.key,
                "label": f"Call {item.key.removeprefix('call')}",
                "value": (
                    item.timing.latency_ms / 1000.0
                    if item.timing.latency_ms is not None
                    else None
                ),
                "status": item.status,
                "http_status": item.http_status,
            }
        )
        cache_series.append(
            {
                "key": item.key,
                "label": f"Call {item.key.removeprefix('call')}",
                "value": item.usage.cache_hit_rate,
                "formula": item.usage.cache_hit_rate_formula,
                "denominator": item.usage.cache_hit_rate_denominator,
                "source": item.usage.cache_hit_rate_source,
                "status": item.status,
            }
        )

        input_tokens = item.usage.input_tokens
        cached = item.usage.cache_read_tokens
        cache_write = item.usage.cache_write_tokens
        if input_tokens is None:
            input_total = uncached = None
        elif (
            item.usage.cache_hit_rate_formula
            and " + " in item.usage.cache_hit_rate_formula
        ):
            input_total = item.usage.cache_hit_rate_denominator
            uncached = input_tokens
        else:
            input_total = input_tokens
            uncached = max(0, input_tokens - (cached or 0))
        output = item.usage.output_tokens
        total = item.usage.total_tokens or (
            input_total + (output or 0) if input_total is not None else None
        )
        token_series.append(
            {
                "key": item.key,
                "label": f"Call {item.key.removeprefix('call')}",
                "input": input_tokens,
                "input_total": input_total,
                "cached": cached,
                "cache_write": cache_write,
                "uncached": uncached,
                "output": output,
                "total": total,
                "formula": item.usage.cache_hit_rate_formula,
                "status": item.status,
                "error": (
                    f"HTTP {item.http_status}"
                    if item.http_status and item.status == "failed"
                    else None
                ),
            }
        )
    return {"latency": latency_series, "cache": cache_series, "tokens": token_series}


def project_summary(
    *,
    facts: OperationFacts,
    model_call_items: list[ModelCallObservationItem],
    timeline: TimelineData,
    trace_data: OperationTraceData | None,
) -> dict[str, Any]:
    """从已经投影的事实构造摘要；不访问外部资源。"""

    duration_ms = timeline.total_duration_ms
    duration_text = (
        f"{duration_ms / 1000.0:.1f}s"
        if duration_ms >= 1000.0
        else f"{round(duration_ms)}ms"
    )
    run_state = facts.run_state
    trace_integrity = (
        trace_data.integrity_summary
        if trace_data is not None
        else "Trace 数据未采集或已清理 · 可靠事实完整"
    )
    return {
        "status": run_state.status if run_state is not None else "unknown",
        "duration_ms": duration_ms,
        "duration_text": duration_text,
        "model_calls_count": len(model_call_items),
        "model_retries_count": sum(item.attempt > 1 for item in model_call_items),
        "tool_calls_count": len(facts.tool_calls),
        "children_count": len(facts.delegations),
        "trace_integrity": trace_integrity,
    }


def project_timeline(
    facts: OperationFacts,
    model_call_items: list[ModelCallObservationItem],
    trace_data: OperationTraceData | None,
    read_session_operations,
    read_run_state,
    tool_call_items: list[ToolCallObservationItem] | None = None,
) -> TimelineData:
    """将可用时间事实映射为时间线；缺失时间只显示零宽未知点。"""

    start = facts.operation.accepted_at
    end = start
    for item in model_call_items:
        for value in (item.timing.started_at, item.timing.finished_at):
            parsed = _parse_iso(value)
            if parsed is not None:
                start = min(start, parsed)
                end = max(end, parsed)
    for span in trace_data.spans if trace_data is not None else ():
        parsed_start = _parse_iso(span.get("started_at"))
        parsed_end = _parse_iso(span.get("finished_at"))
        if parsed_start is not None:
            start = min(start, parsed_start)
        if parsed_end is not None:
            end = max(end, parsed_end)
    for event in trace_data.runtime_events if trace_data is not None else ():
        if str(event.get("event_type") or "") not in {
            "tool_call_started",
            "tool_call_completed",
        }:
            continue
        parsed = _parse_iso(event.get("occurred_at"))
        if parsed is not None:
            start = min(start, parsed)
            end = max(end, parsed)
    total = max(0.0, (end - start).total_seconds() * 1000.0)

    def position(value: str | None) -> float:
        parsed = _parse_iso(value)
        if parsed is None or total <= 0:
            return 0.0
        return max(
            0.0, min(100.0, (parsed - start).total_seconds() * 1000 / total * 100)
        )

    parent_status = _timeline_status(
        facts.run_state.status if facts.run_state is not None else None
    )
    lanes = [
        TimelineLane(
            name="Parent",
            bars=[
                TimelineBar(
                    key="operation",
                    kind="agent",
                    label="Operation",
                    duration_text=f"{total / 1000:.1f}s" if total else "未知",
                    status=parent_status,
                    left_pct=0.0,
                    width_pct=100.0 if total else 0.0,
                )
            ],
        )
    ]
    model_bars = []
    for item in model_call_items:
        left = position(item.timing.started_at or item.timing.created_at)
        right = position(item.timing.finished_at)
        model_bars.append(
            TimelineBar(
                key=item.key,
                kind="model",
                label=f"Call {item.key.removeprefix('call')}",
                duration_text=(
                    f"{item.timing.latency_ms / 1000:.1f}s"
                    if item.timing.latency_ms is not None
                    else "未知"
                ),
                status=(
                    "affected" if item.attempt > 1 else _timeline_status(item.status)
                ),
                left_pct=round(left, 1),
                width_pct=round(max(0.0, right - left), 1),
            )
        )
    lanes.append(TimelineLane(name="Model", bars=model_bars))

    # Tool 的存在性和结果始终来自 ConversationNode；Trace 仅为同一
    # tool_call_id 补充精确开始/结束时间。即使没有 Trace，也必须显示工具点。
    tools = tool_call_items or [
        ToolCallObservationItem.from_fact(item) for item in facts.tool_calls
    ]
    tool_spans: dict[str, dict[str, Any]] = {}
    for span in trace_data.spans if trace_data is not None else ():
        if str(span.get("name", "")) not in {"pickel.tool.execute", "pickel.tool"}:
            continue
        attrs = span.get("attributes")
        attrs = attrs if isinstance(attrs, dict) else {}
        tool_id = str(attrs.get("tool_call_id") or span.get("tool_call_id") or "")
        if tool_id:
            tool_spans[tool_id] = span
    for event in trace_data.runtime_events if trace_data is not None else ():
        event_type = str(event.get("event_type") or "")
        if event_type not in {"tool_call_started", "tool_call_completed"}:
            continue
        tool_id = str(event.get("tool_call_id") or "")
        if not tool_id:
            continue
        span = tool_spans.setdefault(tool_id, {"attributes": {}})
        span.setdefault("attributes", {})
        if event_type == "tool_call_started":
            span["started_at"] = event.get("occurred_at")
        else:
            span["finished_at"] = event.get("occurred_at")
            span["status"] = "error" if event.get("is_error") else "ok"

    tool_bars = []
    for index, tool in enumerate(tools, start=1):
        trace = tool_spans.get(tool.tool_call_id, {})
        started_at = trace.get("started_at")
        finished_at = trace.get("finished_at")
        duration = trace.get("duration_ms")
        if duration is None and _parse_iso(started_at) and _parse_iso(finished_at):
            duration = max(
                0.0,
                (_parse_iso(finished_at) - _parse_iso(started_at)).total_seconds()
                * 1000,
            )
        tool_bars.append(
            TimelineBar(
                key=f"tool_{tool.tool_call_id}",
                kind="tool",
                label=tool.name,
                duration_text=(
                    f"{float(duration) / 1000:.1f}s"
                    if isinstance(duration, (int, float))
                    else "未知"
                ),
                status=(
                    _timeline_status(trace.get("status"))
                    if trace
                    else (
                        "error"
                        if tool.is_error is True
                        else "ok" if tool.result is not None else "unknown"
                    )
                ),
                left_pct=round(position(started_at), 1),
                width_pct=round(
                    max(0.0, position(finished_at) - position(started_at)), 1
                ),
                is_trace=bool(trace),
            )
        )
    lanes.append(TimelineLane(name="Tool", bars=tool_bars))

    storage_bars = []
    for index, item in enumerate(model_call_items, start=1):
        storage_bars.append(
            TimelineBar(
                f"store_req_{index}",
                "storage",
                "RequestContent",
                "已记录",
                "ok",
                round(position(item.timing.created_at), 1),
                0.0,
            )
        )
        if item.response_content_ref and item.timing.finished_at:
            storage_bars.append(
                TimelineBar(
                    f"store_resp_{index}",
                    "storage",
                    "ResponseContent",
                    "已记录",
                    "ok",
                    round(position(item.timing.finished_at), 1),
                    0.0,
                )
            )
    lanes.append(TimelineLane(name="Storage", bars=storage_bars))

    for index, delegation in enumerate(facts.delegations, start=1):
        child_status = "unknown"
        child_operations = read_session_operations(delegation.child_session_id)
        if len(child_operations) == 1:
            child_state = read_run_state(child_operations[0].operation_id)
            if child_state is not None:
                child_status = _timeline_status(child_state.status)
        lanes.append(
            TimelineLane(
                name=f"Child {chr(64 + index)}",
                bars=[
                    TimelineBar(
                        f"child_{index}",
                        "agent",
                        delegation.child_session_id[:8],
                        "未知",
                        child_status,
                        round(position(delegation.created_at.isoformat()), 1),
                        0.0,
                    )
                ],
            )
        )

    if trace_data is not None and (
        trace_data.stream_deltas_count or trace_data.dropped_deltas_count
    ):
        lanes.append(
            TimelineLane(
                name="Stream",
                bars=[
                    TimelineBar(
                        "stream",
                        "model",
                        f"delta × {trace_data.stream_deltas_count}",
                        "未知",
                        "unknown",
                        0.0,
                        0.0,
                        True,
                    )
                ],
            )
        )
    ticks = (
        [f"{total / 5000 * index:.0f}s" for index in range(6)] if total else ["0s"] * 6
    )
    return TimelineData(
        start.isoformat(),
        end.isoformat(),
        total,
        ticks,
        lanes,
        "执行时间线：仅展示已记录的事实",
    )


def project_execution_tree(
    facts: OperationFacts,
    model_call_items: list[ModelCallObservationItem],
    read_session,
    tool_call_items: list[ToolCallObservationItem] | None = None,
) -> list[ExecutionTreeNode]:
    nodes: list[ExecutionTreeNode] = []
    op = facts.operation
    run_state = facts.run_state
    tools = tool_call_items or [
        ToolCallObservationItem.from_fact(item) for item in facts.tool_calls
    ]

    op_status = run_state.status if run_state is not None else "unknown"
    status_flag = _timeline_status(op_status)
    nodes.append(
        ExecutionTreeNode(
            key="operation",
            kind="operation",
            label=f"Operation {op.operation_id[:8]}",
            meta=op_status,
            status=status_flag,
            depth=0,
        )
    )

    # 按 step_sequence 分组 ModelCalls
    steps_map: dict[int, list[ModelCallObservationItem]] = {}
    for item in model_call_items:
        seq = item.step_sequence or 1
        steps_map.setdefault(seq, []).append(item)

    total_steps = max(
        run_state.completed_step_count if run_state else 1,
        max(steps_map.keys()) if steps_map else 1,
    )

    for step_seq in range(1, total_steps + 1):
        calls_in_step = sorted(
            steps_map.get(step_seq, []),
            key=lambda item: (
                item.attempt,
                item.timing.created_at,
                item.model_call_id,
            ),
        )
        step_has_error = any(
            c.status in ("failed", "incomplete") for c in calls_in_step
        )
        step_status = (
            "error" if step_has_error else ("ok" if calls_in_step else "unknown")
        )
        nodes.append(
            ExecutionTreeNode(
                key=f"step_{step_seq}",
                kind="step",
                label=f"Step {step_seq:02d}",
                meta=f"{len(calls_in_step)} calls",
                status=step_status,
                depth=1,
            )
        )

        # ModelCalls 下钻
        for call_item in calls_in_step:
            c_status = (
                "affected"
                if call_item.attempt > 1
                else _timeline_status(call_item.status)
            )
            dur_meta = (
                f"{call_item.timing.latency_ms / 1000:.1f}s"
                if call_item.timing.latency_ms is not None
                else (
                    f"HTTP {call_item.http_status}"
                    if call_item.http_status
                    else call_item.status
                )
            )
            nodes.append(
                ExecutionTreeNode(
                    key=call_item.key,
                    kind="model",
                    label=(
                        f"ModelCall {call_item.attempt:02d}"
                        if call_item.attempt > 1
                        else f"ModelCall {call_item.key.removeprefix('call')}"
                    ),
                    meta=dur_meta,
                    status=c_status,
                    depth=2,
                )
            )

        for tool in (item for item in tools if (item.step_sequence or 1) == step_seq):
            nodes.append(
                ExecutionTreeNode(
                    key=f"tool_{tool.tool_call_id}",
                    kind="tool",
                    label=tool.name,
                    meta=(
                        ("错误" if tool.is_error else "已完成")
                        if tool.result is not None
                        else "等待结果"
                    ),
                    status=(
                        "error"
                        if tool.is_error is True
                        else "ok" if tool.result is not None else "unknown"
                    ),
                    depth=3,
                )
            )

    # Delegations 下钻
    if facts.delegations:
        nodes.append(
            ExecutionTreeNode(
                key="delegation",
                kind="delegation",
                label="Delegation",
                meta=str(len(facts.delegations)),
                status="unknown",
                depth=1,
            )
        )
        for idx, delg in enumerate(facts.delegations, start=1):
            child_session = read_session(delg.child_session_id)
            child_title = (
                child_session.title
                if child_session and child_session.title
                else f"Child {delg.child_session_id[:6]}…"
            )
            nodes.append(
                ExecutionTreeNode(
                    key=f"child_{idx}",
                    kind="child",
                    label=child_title,
                    meta=delg.child_session_id[:8],
                    status="unknown",
                    depth=2,
                )
            )

    return nodes


def project_usage(
    response_content: ResponseContent | None,
    *,
    provider: str,
    api_kind: str,
) -> ModelCallUsageObservation:
    """读取 Provider 返回的 usage，并保留缓存率的计算依据。"""

    if response_content is None:
        return _empty_usage()
    asst_usage = getattr(response_content.assistant_message.metadata, "usage", None)
    provider_response = response_content.provider_response
    provider_usage = (
        provider_response.get("usage")
        if isinstance(provider_response, Mapping)
        else None
    )
    input_tokens = output_tokens = cache_read = cache_write = None
    reasoning_tokens = total_tokens = None
    if asst_usage is not None:
        input_tokens = getattr(asst_usage, "input_tokens", None)
        output_tokens = getattr(asst_usage, "output_tokens", None)
        cache_read = getattr(asst_usage, "cache_read_tokens", None)
        cache_write = getattr(asst_usage, "cache_write_tokens", None)
        reasoning_tokens = getattr(asst_usage, "reasoning_tokens", None)
        total_tokens = getattr(asst_usage, "total_tokens", None)
    source_parts: list[str] = []
    if any(
        value is not None
        for value in (
            input_tokens,
            output_tokens,
            cache_read,
            cache_write,
            reasoning_tokens,
            total_tokens,
        )
    ):
        source_parts.append("assistant_message.metadata.usage")
    if isinstance(provider_usage, Mapping):
        used_provider_usage = False
        if input_tokens is None:
            input_tokens = provider_usage.get(
                "prompt_tokens", provider_usage.get("input_tokens")
            )
            used_provider_usage = input_tokens is not None
        if output_tokens is None:
            output_tokens = provider_usage.get(
                "completion_tokens", provider_usage.get("output_tokens")
            )
            used_provider_usage = used_provider_usage or output_tokens is not None
        if total_tokens is None:
            total_tokens = provider_usage.get("total_tokens")
            used_provider_usage = used_provider_usage or total_tokens is not None
        details = provider_usage.get("prompt_tokens_details")
        if cache_read is None and isinstance(details, Mapping):
            cache_read = details.get("cached_tokens")
            used_provider_usage = used_provider_usage or cache_read is not None
        if used_provider_usage:
            source_parts.append("response_content.provider_response.usage")
    source = " + ".join(source_parts) or None
    rate, denominator, formula = _cache_rate(
        provider=provider,
        api_kind=api_kind,
        input_tokens=input_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )
    return ModelCallUsageObservation(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        reasoning_tokens=reasoning_tokens,
        total_tokens=total_tokens,
        cache_hit_rate=rate,
        cache_hit_rate_formula=formula,
        cache_hit_rate_denominator=denominator,
        cache_hit_rate_source=source,
        provider_reported=(
            dict(provider_usage) if isinstance(provider_usage, Mapping) else None
        ),
    )


def project_finish_reason(response_content: ResponseContent | None) -> str | None:
    if response_content is None:
        return None
    metadata = response_content.assistant_message.metadata
    finish = getattr(metadata, "finish_reason", None)
    if finish:
        return str(finish)
    provider_response = response_content.provider_response
    choices = (
        provider_response.get("choices")
        if isinstance(provider_response, Mapping)
        else None
    )
    if isinstance(choices, (list, tuple)) and choices:
        value = choices[0].get("finish_reason")
        return str(value) if value is not None else None
    return None


def project_model_calls(
    calls: tuple[ModelCall, ...] | list[ModelCall],
    content_reader: ModelCallContentReader,
) -> list[ModelCallObservationItem]:
    # 1. 投影 ModelCalls
    model_call_items: list[ModelCallObservationItem] = []
    # Store 的默认顺序通常按 (step, attempt)，但 attempt 会在每个 Step
    # 重置；观测图表必须按真实创建时间排序，避免跨 Step 倒序。
    ordered_calls = sorted(calls, key=_model_call_sort_key)
    for index, call in enumerate(ordered_calls, start=1):
        key = f"call{index}"
        req_res = content_reader.read_request_content(call.request_content_ref)
        resp_res = content_reader.read_response_content(call.response_content_ref)

        latency_ms = None
        if call.started_at and call.finished_at:
            latency_ms = max(
                0.0,
                round(
                    (call.finished_at - call.started_at).total_seconds() * 1000,
                    2,
                ),
            )
        ttft_ms = None
        if call.started_at and call.first_chunk_at:
            ttft_ms = max(
                0.0,
                round(
                    (call.first_chunk_at - call.started_at).total_seconds() * 1000,
                    2,
                ),
            )

        timing = ModelCallTimingObservation(
            created_at=call.created_at.isoformat(),
            started_at=(
                call.started_at.isoformat() if call.started_at is not None else None
            ),
            first_chunk_at=(
                call.first_chunk_at.isoformat()
                if call.first_chunk_at is not None
                else None
            ),
            finished_at=(
                call.finished_at.isoformat() if call.finished_at is not None else None
            ),
            latency_ms=latency_ms,
            ttft_ms=ttft_ms,
        )

        # Usage 计算
        usage_obs = project_usage(
            resp_res.content,
            provider=call.provider,
            api_kind=call.api_kind,
        )
        finish_reason = project_finish_reason(resp_res.content)

        model_call_items.append(
            ModelCallObservationItem(
                key=key,
                model_call_id=call.model_call_id,
                session_id=call.session_id,
                operation_id=call.operation_id,
                step_id=call.step_id,
                step_sequence=call.step_sequence,
                attempt=call.request_attempt,
                model_role=call.model_role,
                purpose=call.purpose,
                provider=call.provider,
                api_kind=call.api_kind,
                endpoint=call.endpoint,
                requested_model=call.requested_model,
                returned_model=call.returned_model,
                status=call.status,
                context_fingerprint=call.context_fingerprint,
                provider_request_id=call.provider_request_id,
                http_status=call.http_status,
                request_content_ref=call.request_content_ref,
                response_content_ref=call.response_content_ref,
                request_content=req_res,
                response_content=resp_res,
                timing=timing,
                usage=usage_obs,
                finish_reason=finish_reason,
                error=asdict(call.error) if call.error is not None else None,
            )
        )

    return model_call_items


def project_tool_calls(
    facts: OperationFacts,
    model_call_items: list[ModelCallObservationItem],
) -> list[ToolCallObservationItem]:
    """投影工具事实，并以 ResponseContent 中的 ToolCallBlock 精确归属 Step。"""
    result: list[ToolCallObservationItem] = []
    for fact in facts.tool_calls:
        item = ToolCallObservationItem.from_fact(fact)
        if item.step_id is None:
            for model_call in model_call_items:
                response = model_call.response_content.content
                if response is None:
                    continue
                if any(
                    isinstance(block, ToolCallBlock) and block.id == item.tool_call_id
                    for block in response.assistant_message.content
                ):
                    item = replace(
                        item,
                        step_id=model_call.step_id,
                        step_sequence=model_call.step_sequence,
                    )
                    break
        result.append(item)
    return result


def project_model_call_evidence(
    model_call_items: list[ModelCallObservationItem],
) -> dict[str, Any]:
    evidence_by_call: dict[str, Any] = {}

    for item in model_call_items:
        req_content = item.request_content.content
        resp_content = item.response_content.content

        # ModelContext 分层
        ctx_sections = []
        if req_content:
            ctx = req_content.model_context
            # system
            sys_blocks = [{"name": s.name, "text": s.text} for s in ctx.system.sections]
            ctx_sections.append(
                {
                    "id": "system",
                    "label": "system",
                    "count": str(len(ctx.system.sections)),
                    "path": "model_context.system",
                    "complete": f"{len(ctx.system.sections)} system sections · 完整内容",
                    "value": {"sections": sys_blocks},
                }
            )
            # messages
            msg_list = [agent_message_to_dict(m) for m in ctx.messages]
            ctx_sections.append(
                {
                    "id": "messages",
                    "label": "messages",
                    "count": str(len(ctx.messages)),
                    "path": f"model_context.messages[{len(ctx.messages)}]",
                    "complete": f"{len(ctx.messages)} 条消息 · 完整未截断",
                    "value": msg_list,
                }
            )
            # tools
            tool_list = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                    "output_schema": t.output_schema,
                }
                for t in ctx.tools
            ]
            ctx_sections.append(
                {
                    "id": "tools",
                    "label": "tools",
                    "count": str(len(ctx.tools)),
                    "path": "model_context.tools",
                    "complete": f"{len(ctx.tools)} tool definitions · 完整未截断",
                    "value": tool_list,
                }
            )
        else:
            ctx_sections.append(
                {
                    "id": "system",
                    "label": "system",
                    "count": "0",
                    "path": "model_context",
                    "complete": item.request_content.error or "未找到 RequestContent",
                    "value": None,
                }
            )

        # Wire Request 分层
        wire_sections = []
        if req_content:
            wire = req_content.wire_request
            wire_sections.append(
                {
                    "id": "body",
                    "label": "request body",
                    "count": "完整",
                    "path": "wire_request",
                    "complete": "发送字节对应对象 · 未重建",
                    "value": wire,
                }
            )
            if "messages" in wire:
                wire_sections.append(
                    {
                        "id": "wire-messages",
                        "label": "messages",
                        "count": str(
                            len(wire["messages"])
                            if isinstance(wire["messages"], (list, tuple))
                            else "0"
                        ),
                        "path": "wire_request.messages",
                        "complete": "Provider wire 映射结果 · 未截断",
                        "value": wire["messages"],
                    }
                )
            if "tools" in wire:
                wire_sections.append(
                    {
                        "id": "wire-tools",
                        "label": "tools",
                        "count": str(
                            len(wire["tools"])
                            if isinstance(wire["tools"], (list, tuple))
                            else "0"
                        ),
                        "path": "wire_request.tools",
                        "complete": "仅含 Provider 协议字段",
                        "value": wire["tools"],
                    }
                )
        else:
            wire_sections.append(
                {
                    "id": "body",
                    "label": "request body",
                    "count": "0",
                    "path": "wire_request",
                    "complete": item.request_content.error or "未找到 Wire Request",
                    "value": None,
                }
            )

        # Provider Response 分层
        prov_sections = []
        if resp_content:
            prov_resp = resp_content.provider_response
            choices = prov_resp.get("choices") if isinstance(prov_resp, Mapping) else ()
            first_choice = (
                choices[0] if isinstance(choices, (list, tuple)) and choices else {}
            )
            first_message = (
                first_choice.get("message", {})
                if isinstance(first_choice, Mapping)
                else {}
            )
            raw_events = None
            if isinstance(prov_resp, Mapping):
                for key in ("raw_events", "events", "stream", "deltas"):
                    if key in prov_resp:
                        raw_events = prov_resp[key]
                        break
            summary_value = {
                key: prov_resp[key]
                for key in ("id", "model", "object", "created", "finish_reason")
                if isinstance(prov_resp, Mapping) and key in prov_resp
            }
            prov_sections.append(
                {
                    "id": "summary",
                    "label": "summary",
                    "count": "聚合",
                    "path": "provider_response",
                    "complete": "Provider 响应摘要",
                    "value": summary_value,
                }
            )
            # 保留完整 provider response；以下小节只是导航，不是另一份事实。
            prov_sections.append(
                {
                    "id": "provider-body",
                    "label": "response",
                    "count": "完整",
                    "path": "provider_response",
                    "complete": "聚合 Provider 响应 · 未截断",
                    "value": prov_resp,
                }
            )
            if isinstance(prov_resp, Mapping) and "usage" in prov_resp:
                prov_sections.append(
                    {
                        "id": "usage",
                        "label": "usage",
                        "count": "原始",
                        "path": "provider_response.usage",
                        "complete": "Provider 原始计数",
                        "value": prov_resp["usage"],
                    }
                )
            tool_values = None
            if isinstance(prov_resp, Mapping):
                tool_values = prov_resp.get("tool_calls")
            if tool_values is None and isinstance(first_message, Mapping):
                tool_values = first_message.get("tool_calls")
            if tool_values is not None:
                prov_sections.append(
                    {
                        "id": "tool_calls",
                        "label": "tool calls",
                        "count": (
                            str(len(tool_values))
                            if isinstance(tool_values, (list, tuple))
                            else "1"
                        ),
                        "path": "provider_response.tool_calls",
                        "complete": "Provider 原始工具调用",
                        "value": tool_values,
                    }
                )
            reasoning_value = (
                first_message.get("reasoning_content") or first_message.get("reasoning")
                if isinstance(first_message, Mapping)
                else None
            )
            text_value = (
                first_message.get("content")
                if isinstance(first_message, Mapping)
                else None
            )
            for section_id, label, path, value in (
                (
                    "reasoning",
                    "reasoning",
                    "provider_response.choices[0].message.reasoning",
                    reasoning_value,
                ),
                (
                    "text",
                    "text",
                    "provider_response.choices[0].message.content",
                    text_value,
                ),
            ):
                if value is not None:
                    prov_sections.append(
                        {
                            "id": section_id,
                            "label": label,
                            "count": "聚合",
                            "path": path,
                            "complete": "Provider 原始内容聚合",
                            "value": value,
                        }
                    )
            if raw_events is not None:
                prov_sections.append(
                    {
                        "id": "raw-events",
                        "label": "raw events",
                        "count": (
                            str(len(raw_events))
                            if isinstance(raw_events, (list, tuple))
                            else "完整"
                        ),
                        "path": "provider_response.raw_events",
                        "complete": "原始流式事件 · 未丢弃",
                        "value": raw_events,
                    }
                )
        else:
            prov_sections.append(
                {
                    "id": "provider-body",
                    "label": "response",
                    "count": "0",
                    "path": "provider_response",
                    "complete": item.response_content.error or "无 ResponseContent",
                    "value": None,
                }
            )

        # AssistantMessage 分层
        asst_sections = []
        if resp_content and resp_content.assistant_message:
            asst_msg = resp_content.assistant_message
            asst_sections.append(
                {
                    "id": "assistant-message",
                    "label": "message",
                    "count": "完整",
                    "path": "assistant_message",
                    "complete": "Provider-neutral 规范消息",
                    "value": agent_message_to_dict(asst_msg),
                }
            )
            asst_sections.append(
                {
                    "id": "metadata",
                    "label": "metadata",
                    "count": "用量",
                    "path": "assistant_message.metadata",
                    "complete": "Token 面板的数据来源",
                    "value": (
                        asdict(asst_msg.metadata)
                        if hasattr(asst_msg, "metadata") and asst_msg.metadata
                        else {}
                    ),
                }
            )
        else:
            asst_sections.append(
                {
                    "id": "assistant-message",
                    "label": "message",
                    "count": "0",
                    "path": "assistant_message",
                    "complete": item.response_content.error or "无 AssistantMessage",
                    "value": None,
                }
            )

        evidence_by_call[item.key] = {
            "label": f"ModelCall {item.key.removeprefix('call')}",
            "model_call_id": item.model_call_id,
            "request_content_ref": item.request_content_ref,
            "response_content_ref": item.response_content_ref or "—",
            "status": item.status,
            "attempt": item.attempt,
            "context_fingerprint": item.context_fingerprint or "—",
            "context": {
                "label": "RequestContent.model_context",
                "schema_version": (
                    req_content.schema_version if req_content is not None else None
                ),
                "canonical_bytes_verified": (
                    True if item.request_content.is_ok else False
                ),
                "error": item.request_content.error,
                "sections": ctx_sections,
            },
            "wire": {
                "label": "RequestContent.wire_request",
                "schema_version": (
                    req_content.schema_version if req_content is not None else None
                ),
                "canonical_bytes_verified": (
                    True if item.request_content.is_ok else False
                ),
                "error": item.request_content.error,
                "sections": wire_sections,
            },
            "provider": {
                "label": "ResponseContent.provider_response",
                "schema_version": (
                    resp_content.schema_version if resp_content is not None else None
                ),
                "canonical_bytes_verified": (
                    True
                    if item.response_content.is_ok
                    else False if item.response_content.error is not None else None
                ),
                "error": item.response_content.error,
                "sections": prov_sections,
            },
            "assistant": {
                "label": "ResponseContent.assistant_message",
                "schema_version": (
                    resp_content.schema_version if resp_content is not None else None
                ),
                "canonical_bytes_verified": (
                    True
                    if item.response_content.is_ok
                    else False if item.response_content.error is not None else None
                ),
                "error": item.response_content.error,
                "sections": asst_sections,
            },
        }

    # 该函数也被独立 Evidence API 直接复用，因此返回值本身必须是 JSON
    # 数据合同，不能把冻结层的 MappingProxyType/tuple 泄漏到传输层。
    return _json_serializable(evidence_by_call)


class OperationObservationProjector:
    """组合可靠事实、ModelCall 内容与 Trace 生成诊断工作台展示文档。"""

    def __init__(
        self,
        fact_reader: OperationFactReader,
        content_reader: ModelCallContentReader,
    ) -> None:
        self._facts = fact_reader
        self._content = content_reader

    def project_operation(
        self,
        operation_id: str,
        *,
        trace_data: OperationTraceData | None = None,
        include_evidence: bool = True,
    ) -> OperationObservationDocument:
        facts = self._facts.read_operation_facts(operation_id)
        if facts is None:
            raise ValueError(f"未找到 Operation: {operation_id}")

        session = self._facts.read_session(facts.operation.session_id)
        if session is None:
            raise ValueError(f"未找到 Session: {facts.operation.session_id}")

        session_dict = {
            "session_id": session.session_id,
            "agent_id": session.agent_id,
            "workspace_id": session.workspace_id,
            "cwd": str(session.cwd),
            "active_node_id": session.active_node_id,
            "active_operation_id": session.active_operation_id,
            "title": session.title,
            "title_source": session.title_source,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "archived_at": (
                session.archived_at.isoformat()
                if session.archived_at is not None
                else None
            ),
        }

        op = facts.operation
        run_state = facts.run_state
        op_dict = {
            "operation_id": op.operation_id,
            "session_id": op.session_id,
            "agent_package_version_id": op.agent_package_version_id,
            "workspace_binding": {
                "workspace_id": op.workspace_binding.workspace_id,
                "working_directory": str(op.workspace_binding.working_directory),
                "allowed_root": (
                    str(op.workspace_binding.allowed_root)
                    if op.workspace_binding.allowed_root is not None
                    else None
                ),
            },
            "input_node_id": op.input_node_id,
            "accepted_at": op.accepted_at.isoformat(),
            "status": run_state.status if run_state is not None else "unknown",
            "revision": run_state.revision if run_state is not None else 0,
            "waiting_reason": (
                run_state.waiting_reason if run_state is not None else None
            ),
            "completed_step_count": (
                run_state.completed_step_count if run_state is not None else 0
            ),
            "final_assistant_node_id": (
                run_state.final_assistant_node_id if run_state is not None else None
            ),
            "error": (
                asdict(run_state.error)
                if run_state is not None and run_state.error is not None
                else None
            ),
            "cancellation": (
                asdict(run_state.cancellation)
                if run_state is not None and run_state.cancellation is not None
                else None
            ),
            "updated_at": op.accepted_at.isoformat(),
        }

        # 1. 投影 ModelCalls
        model_call_items = project_model_calls(facts.model_calls, self._content)
        tool_call_items = project_tool_calls(facts, model_call_items)

        # 2. 构造执行树节点
        execution_nodes = project_execution_tree(
            facts, model_call_items, self._facts.read_session, tool_call_items
        )

        # 3. 构造统一时间线
        timeline = project_timeline(
            facts,
            model_call_items,
            trace_data,
            self._facts.read_session_operations,
            self._facts.read_run_state,
            tool_call_items,
        )

        # 4. 图表数据
        charts = project_charts(model_call_items)

        # 5. 第三层不可变证据文档映射
        document_evidence = (
            project_model_call_evidence(model_call_items) if include_evidence else {}
        )

        # 6. Summary
        summary = project_summary(
            facts=facts,
            model_call_items=model_call_items,
            timeline=timeline,
            trace_data=trace_data,
        )
        trace_integrity = summary["trace_integrity"]

        return OperationObservationDocument(
            session=session_dict,
            operation=op_dict,
            summary=summary,
            model_calls=model_call_items,
            execution_nodes=execution_nodes,
            timeline=timeline,
            charts=charts,
            document_evidence=document_evidence,
            trace_integrity=trace_integrity,
            tool_calls=tool_call_items,
        )
