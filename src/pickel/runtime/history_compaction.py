"""HistoryCompaction 的 worker 调用与持久化编排。

纯选择算法留在 context；涉及 ModelCall、Provider 和 Store 的副作用属于
Runtime。这里只移动既有行为，不定义新的压缩策略。
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Sequence

from pickel.context.history_compaction import plan_history_compaction_for_budget
from pickel.context.model_context import ModelContext, SystemContent
from pickel.conversations.agent_message import (
    AgentMessage,
    UserMessage,
    agent_message_to_dict,
)
from pickel.conversations.conversation_node import ConversationNode, HistoryCompaction
from pickel.conversations.conversation_service import ConversationService
from pickel.conversations.content_blocks import TextBlock
from pickel.model_calls.service import ModelCallService
from pickel.providers.errors import classify_provider_error
from pickel.runtime.model_call_send_gate import ModelCallSendFailure, ModelCallSendGate
from pickel.runtime.runtime_effects import RuntimeEffects


class HistoryCompactionError(RuntimeError):
    """压缩不能安全完成；调用方应停止当前 Step。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class HistoryCompactionService:
    """使用冻结 worker ModelPolicy 生成并提交一次压缩事实。"""

    def __init__(
        self,
        *,
        conversation_service: ConversationService,
        model_calls: ModelCallService | None,
        send_gate: ModelCallSendGate | None,
    ) -> None:
        self._conversations = conversation_service
        self._model_calls = model_calls
        self._send_gate = send_gate

    async def compact(
        self,
        *,
        session_id: str,
        nodes: Sequence[ConversationNode],
        package: Any,
        effects: RuntimeEffects,
        target_token_budget: int,
    ) -> ConversationNode:
        worker_model = getattr(getattr(package, "model_policy", None), "worker", None)
        worker = getattr(effects, "worker_provider", None)
        if worker_model is None or worker is None:
            raise HistoryCompactionError(
                "history_compaction_worker_unavailable",
                "当前冻结 Package 未配置可用的 worker 模型，无法压缩历史",
            )
        if self._model_calls is None or self._send_gate is None:
            raise HistoryCompactionError(
                "history_compaction_model_call_unavailable",
                "当前 Runtime 未配置 ModelCall 服务，无法压缩历史",
            )
        plan = plan_history_compaction_for_budget(
            nodes, target_token_budget=target_token_budget
        )
        if plan is None or not plan.messages:
            raise HistoryCompactionError(
                "history_compaction_no_progress",
                "当前 Context 没有可压缩的完整消息单元",
            )
        prompt = (
            "请将以下历史消息压缩为准确、简洁的工作摘要。保留已完成事实、错误、"
            "文件变化、约束和未完成事项；不要虚构内容，只输出摘要文本。\n\n"
            + "\n".join(
                json.dumps(
                    agent_message_to_dict(message),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                for message in plan.messages
            )
        )
        context = ModelContext(
            system=SystemContent.from_text("你是历史压缩 worker。"),
            messages=(UserMessage(content=(TextBlock(prompt),)),),
        )
        try:
            prepared = self._model_calls.prepare_session_call(
                session_id=session_id,
                context=context,
                mapper=worker,
                request_attempt=1,
                model_role="worker",
                purpose="history_compaction",
            )
            worker_effects = replace(
                effects,
                provider=worker,
                provider_name=worker_model.provider,
                model_name=worker_model.model,
            )
            response = await self._send_gate.send(
                call=prepared.model_call,
                prepared=prepared.prepared,
                effects=worker_effects,
            )
            self._model_calls.complete_session_response(
                call=prepared.model_call, response=response
            )
        except ModelCallSendFailure as exc:
            error = classify_provider_error(exc.cause)
            self._model_calls.mark_failed(
                exc.call, error, first_chunk_at=exc.first_chunk_at
            )
            raise HistoryCompactionError(
                "history_compaction_provider_failed", str(error)
            ) from exc
        except Exception as exc:
            raise HistoryCompactionError(
                "history_compaction_failed", f"历史压缩失败: {exc}"
            ) from exc
        summary = _message_text(response.assistant_message).strip()
        if not summary:
            raise HistoryCompactionError(
                "history_compaction_empty_summary", "worker 未返回有效历史摘要"
            )
        try:
            return self._conversations.append_history_compaction(
                session_id=session_id,
                content=HistoryCompaction(
                    summary=summary,
                    first_kept_node_id=plan.first_kept_node_id,
                ),
            )
        except Exception as exc:
            raise HistoryCompactionError(
                "history_compaction_commit_failed", f"历史压缩提交失败: {exc}"
            ) from exc


def _message_text(message: AgentMessage) -> str:
    return "\n".join(
        getattr(block, "text", "")
        for block in getattr(message, "content", ())
        if isinstance(block, TextBlock)
    )
