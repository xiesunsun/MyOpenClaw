"""将一个 Session 的可靠事实投影为轻量级 index。

Index 只用于 Session/Operation 选择器和汇总面板；ModelContext、Provider
Response 等大证据必须通过 Operation 详情按需读取。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pickel.observe.model_call_content_reader import ModelCallContentReader
from pickel.observe.operation_fact_reader import OperationFactReader
from pickel.observe.operation_projector import (
    ModelCallUsageObservation,
    project_usage,
    project_usage_summary,
)


def _session_dict(session: Any) -> dict[str, Any]:
    return {
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
            session.archived_at.isoformat() if session.archived_at is not None else None
        ),
    }


@dataclass(frozen=True)
class SessionObservationIndex:
    session: dict[str, Any]
    aggregate: dict[str, Any]
    operations: list[dict[str, Any]]
    trace_status: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "scope": "session",
            "session": self.session,
            "aggregate": self.aggregate,
            "operations": self.operations,
            "trace_status": self.trace_status,
        }

    def to_json(self) -> str:
        import json

        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, default=str)


class SessionObservationProjector:
    """Session index 的纯组合投影器，不写入 Store。"""

    def __init__(
        self,
        fact_reader: OperationFactReader,
        content_reader: ModelCallContentReader,
        *,
        operation_url: str = "/api/v1/operations/{operation_id}",
    ) -> None:
        self._facts = fact_reader
        self._content = content_reader
        self._operation_url = operation_url

    def project_session(
        self,
        session_id: str,
        *,
        trace_status: Any = None,
    ) -> SessionObservationIndex:
        session = self._facts.read_session(session_id)
        if session is None:
            raise ValueError(f"未找到 Session: {session_id}")

        operation_items: list[dict[str, Any]] = []
        totals: dict[str, int] = {
            "operations_count": 0,
            "model_calls_count": 0,
            "tool_calls_count": 0,
            "children_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
        }
        known: dict[str, bool] = {
            key: False
            for key in totals
            if key
            not in {
                "operations_count",
                "model_calls_count",
                "tool_calls_count",
                "children_count",
            }
        }
        cache_denominator = 0
        cache_cached = 0
        agent_usages: list[ModelCallUsageObservation] = []
        agent_statuses: list[str] = []
        workflow_usages: list[ModelCallUsageObservation] = []
        workflow_statuses: list[str] = []
        for operation in sorted(
            self._facts.read_session_operations(session_id),
            key=lambda item: (item.accepted_at, item.operation_id),
        ):
            facts = self._facts.read_operation_facts(operation.operation_id)
            if facts is None:
                continue
            usages = [
                project_usage(
                    self._content.read_response_content(
                        call.response_content_ref
                    ).content,
                    provider=call.provider,
                    api_kind=call.api_kind,
                )
                for call in facts.model_calls
            ]
            agent_usages.extend(usages)
            agent_statuses.extend(call.status for call in facts.model_calls)
            workflow_facts = _collect_workflow_facts(self._facts, facts)
            workflow_call_usages: list[ModelCallUsageObservation] = []
            workflow_call_statuses: list[str] = []
            for workflow_fact in workflow_facts:
                for call in workflow_fact.model_calls:
                    usage = project_usage(
                        self._content.read_response_content(
                            call.response_content_ref
                        ).content,
                        provider=call.provider,
                        api_kind=call.api_kind,
                    )
                    workflow_call_usages.append(usage)
                    workflow_call_statuses.append(call.status)
            workflow_usages.extend(workflow_call_usages)
            workflow_statuses.extend(workflow_call_statuses)
            status = (
                facts.run_state.status if facts.run_state is not None else "unknown"
            )
            model_calls_count = len(facts.model_calls)
            tool_calls_count = len(facts.tool_calls)
            children_count = len(facts.delegations)
            totals["operations_count"] += 1
            totals["model_calls_count"] += model_calls_count
            totals["tool_calls_count"] += tool_calls_count
            totals["children_count"] += children_count
            for usage in usages:
                for field in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                    "total_tokens",
                ):
                    value = getattr(usage, field)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        totals[field] += int(value)
                        known[field] = True
                if (
                    usage.cache_hit_rate_denominator is not None
                    and usage.cache_read_tokens is not None
                ):
                    cache_denominator += usage.cache_hit_rate_denominator
                    cache_cached += usage.cache_read_tokens

            operation_items.append(
                {
                    "id": operation.operation_id,
                    "operation_id": operation.operation_id,
                    "status": status,
                    "revision": (
                        facts.run_state.revision if facts.run_state is not None else 0
                    ),
                    "accepted_at": operation.accepted_at.isoformat(),
                    "duration_ms": _operation_duration_ms(operation, facts.model_calls),
                    # Session index 没有按 Operation 读取 Trace；因此只提供
                    # 可由 ConversationNode 复算的答案时间，完成时间明确未知。
                    "answer_ready_ms": _answer_ready_ms(facts),
                    "operation_completed_ms": None,
                    "model_calls_count": model_calls_count,
                    "tool_calls_count": tool_calls_count,
                    "children_count": children_count,
                    "input_tokens": _sum_usage(usages, "input_tokens"),
                    "output_tokens": _sum_usage(usages, "output_tokens"),
                    "cache_read_tokens": _sum_usage(usages, "cache_read_tokens"),
                    "cache_hit_rate": _aggregate_cache_rate(usages),
                    "usage": {
                        "agent": project_usage_summary(
                            usages, statuses=(call.status for call in facts.model_calls)
                        ),
                        "workflow-inclusive": project_usage_summary(
                            workflow_call_usages, statuses=workflow_call_statuses
                        ),
                    },
                    "usage_unknown_attempt_count": sum(
                        1 for usage in usages if _usage_is_unknown(usage)
                    ),
                    "failed_usage_unknown_attempt_count": sum(
                        1
                        for call, usage in zip(facts.model_calls, usages)
                        if call.status in {"failed", "error"}
                        and _usage_is_unknown(usage)
                    ),
                    "operation_url": self._operation_url.format(
                        operation_id=operation.operation_id
                    ),
                }
            )

        for field, is_known in known.items():
            if not is_known:
                totals[field] = None  # type: ignore[assignment]
        totals["cache_hit_rate"] = (
            round(cache_cached / cache_denominator * 100, 2)
            if cache_denominator
            else None
        )
        totals["usage"] = {
            "agent": project_usage_summary(agent_usages, statuses=agent_statuses),
            "workflow-inclusive": project_usage_summary(
                workflow_usages, statuses=workflow_statuses
            ),
        }
        totals.update(
            {
                "operations": totals["operations_count"],
                "model_calls": totals["model_calls_count"],
                "tool_calls": totals["tool_calls_count"],
                "children": totals["children_count"],
            }
        )
        if trace_status is None:
            trace_status = {
                "available": False,
                "message": "Trace 按 Operation 按需加载",
            }
        return SessionObservationIndex(
            session=_session_dict(session),
            aggregate=totals,
            operations=operation_items,
            trace_status=trace_status,
        )


def _usage_is_unknown(usage: ModelCallUsageObservation) -> bool:
    return not any(
        isinstance(getattr(usage, field), int)
        and not isinstance(getattr(usage, field), bool)
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "total_tokens",
        )
    )


def _answer_ready_ms(facts: Any) -> float | None:
    input_node = facts.input_node
    final_node = facts.final_node
    if input_node is None or final_node is None:
        return None
    return round(
        max(
            0.0, (final_node.created_at - input_node.created_at).total_seconds() * 1000
        ),
        3,
    )


def _collect_workflow_facts(reader: OperationFactReader, root: Any) -> list[Any]:
    """读取 root Operation 及其 durable Child 后代，防止循环关系扩散。"""

    result: list[Any] = []
    visited: set[str] = set()

    def visit(facts: Any) -> None:
        operation_id = facts.operation.operation_id
        if operation_id in visited:
            return
        visited.add(operation_id)
        result.append(facts)
        for delegation in facts.delegations:
            for child_operation in reader.read_session_operations(
                delegation.child_session_id
            ):
                child_facts = reader.read_operation_facts(child_operation.operation_id)
                if child_facts is not None:
                    visit(child_facts)

    visit(root)
    return result


def _sum_usage(items: list[ModelCallUsageObservation], field: str) -> int | None:
    values = [getattr(item, field) for item in items]
    numeric = [
        value
        for value in values
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    return sum(numeric) if numeric else None


def _aggregate_cache_rate(items: list[ModelCallUsageObservation]) -> float | None:
    denominator = 0
    cached = 0
    for item in items:
        if item.cache_hit_rate_denominator is None or item.cache_read_tokens is None:
            continue
        denominator += item.cache_hit_rate_denominator
        cached += item.cache_read_tokens
    return round(cached / denominator * 100, 2) if denominator else None


def _operation_duration_ms(operation: Any, model_calls: tuple[Any, ...]) -> float:
    finished = [
        call.finished_at for call in model_calls if call.finished_at is not None
    ]
    if not finished:
        return 0.0
    return round(
        max(0.0, (max(finished) - operation.accepted_at).total_seconds() * 1000.0),
        3,
    )
