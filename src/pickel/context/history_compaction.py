"""HistoryCompaction 的生成协议与 worker 实现。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Callable
from typing import Protocol

from pickel.context.model_context import ModelContext, SystemContent, SystemSection
from pickel.context.token_preflight import TokenPreflightResult
from pickel.conversations.agent_message import (
    AssistantMessage,
    UserMessage,
    agent_message_to_dict,
)
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_node import ConversationNode, HistoryCompaction
from pickel.model_calls.service import ModelCallResponse, ModelCallService
from pickel.providers.errors import (
    ProviderStreamIncompleteError,
    classify_provider_error,
)
from pickel.runtime.model_call_send_gate import ModelCallSendFailure, ModelCallSendGate
from pickel.runtime.runtime_effects import RuntimeEffects


class HistoryCompactionError(RuntimeError):
    """Generator 无法安全地产出 HistoryCompaction。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class HistoryCompactionGenerator(Protocol):
    """从一次明确的压缩请求生成 Conversation 内容值。"""

    async def generate(
        self,
        *,
        session_id: str,
        nodes: Sequence[ConversationNode],
        model_context: ModelContext,
        preflight: TokenPreflightResult,
        runtime_effects: RuntimeEffects | None = None,
    ) -> HistoryCompaction:
        """返回待追加的值；不得删除或改写已有 ConversationNode。"""
        ...


class ModelBackedHistoryCompactionGenerator:
    """使用冻结 Package 的 worker model 生成历史压缩节点。"""

    def __init__(
        self,
        *,
        model_calls: ModelCallService,
        send_gate: ModelCallSendGate,
        now: Callable[[], datetime] | None = None,
        preserve_tail_tokens: int = 32_000,
        summary_input_tokens: int = 64_000,
    ) -> None:
        if preserve_tail_tokens < 1 or summary_input_tokens < 1:
            raise ValueError("压缩 token 预算必须大于 0")
        self._model_calls = model_calls
        self._send_gate = send_gate
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._preserve_tail_tokens = preserve_tail_tokens
        self._summary_input_tokens = summary_input_tokens

    async def generate(
        self,
        *,
        session_id: str,
        nodes: Sequence[ConversationNode],
        model_context: ModelContext,
        preflight: TokenPreflightResult,
        runtime_effects: RuntimeEffects | None = None,
    ) -> HistoryCompaction:
        del model_context, preflight
        if runtime_effects is None or runtime_effects.worker_provider is None:
            raise HistoryCompactionError(
                "history_compaction_unavailable",
                "Context 压缩需要配置 worker model",
            )
        first_kept_index = self._first_kept_index(nodes)
        if first_kept_index is None:
            raise HistoryCompactionError(
                "history_compaction_no_history",
                "没有足够的历史消息可以压缩",
            )
        first_kept = nodes[first_kept_index]
        old_nodes = nodes[:first_kept_index]
        prompt = self._summary_prompt(old_nodes)
        context = ModelContext(
            system=SystemContent(
                sections=(
                    SystemSection(
                        name="history_compaction",
                        text=(
                            "你是代码 Agent 的历史压缩 worker。只输出简洁的中文事实摘要，"
                            "不得调用工具、不得臆测。必须保留用户目标、约束、已经验证的命令与结果、"
                            "修改过的文件、错误、未完成事项和下一步。"
                        ),
                    ),
                )
            ),
            messages=(UserMessage((TextBlock(prompt),)),),
            tools=(),
        )
        provider = runtime_effects.worker_provider
        prepared_call = self._model_calls.prepare_session_call(
            session_id=session_id,
            context=context,
            mapper=provider,
            request_attempt=1,
            model_role="worker",
            purpose="history_compaction",
        )
        worker_effects = RuntimeEffects(
            provider=provider,
            provider_name=prepared_call.prepared.provider,
            model_name=prepared_call.prepared.requested_model,
            provider_timeout_seconds=runtime_effects.provider_timeout_seconds,
        )
        try:
            response = await self._send_gate.send(
                call=prepared_call.model_call,
                prepared=prepared_call.prepared,
                effects=worker_effects,
            )
        except ModelCallSendFailure as exc:
            self._record_failure(exc)
            raise HistoryCompactionError(
                "history_compaction_failed",
                f"worker 压缩请求失败: {exc}",
            ) from exc
        self._model_calls.complete_session_response(
            call=prepared_call.model_call,
            response=response,
        )
        summary = self._summary_text(response.assistant_message)
        if not summary:
            raise HistoryCompactionError(
                "history_compaction_empty",
                "worker 压缩响应为空",
            )
        return HistoryCompaction(
            summary=summary,
            first_kept_node_id=first_kept.node_id,
        )

    def _first_kept_index(self, nodes: Sequence[ConversationNode]) -> int | None:
        if len(nodes) < 2:
            return None
        total = 0
        first = len(nodes) - 1
        for index in range(len(nodes) - 1, -1, -1):
            node = nodes[index]
            if node.content_type != "agent_message":
                continue
            cost = max(1, len(json.dumps(agent_message_to_dict(node.content))) // 4)
            if index < len(nodes) - 1 and total + cost > self._preserve_tail_tokens:
                break
            total += cost
            first = index
        # 活动分支不能从孤立的 Assistant 开始，否则模型会看到没有对应问题的
        # 答案。优先把紧邻的 User 一并保留；ToolResult 组由后续协议继续完善。
        if (
            first < len(nodes)
            and isinstance(nodes[first].content, AssistantMessage)
            and first > 0
            and isinstance(nodes[first - 1].content, UserMessage)
        ):
            first -= 1
        return first if first > 0 else None

    def _summary_prompt(self, nodes: Sequence[ConversationNode]) -> str:
        rendered = []
        for index, node in enumerate(nodes):
            rendered.append(
                f"[{index}] {json.dumps(self._node_payload(node), ensure_ascii=False)}"
            )
        text = "\n\n".join(rendered)
        limit = self._summary_input_tokens * 4
        if len(text) <= limit:
            return "请压缩以下历史消息：\n\n" + text
        head = limit // 2
        tail = limit - head
        return (
            "请压缩以下历史消息。中间部分因压缩 worker 输入预算被省略，"
            "不要假设省略部分的具体内容：\n\n"
            + text[:head]
            + "\n\n[中间历史已省略]\n\n"
            + text[-tail:]
        )

    @staticmethod
    def _node_payload(node: ConversationNode) -> dict:
        if node.content_type == "agent_message":
            return agent_message_to_dict(node.content)
        content = node.content
        return {
            "content_type": "history_compaction",
            "summary": content.summary,
            "first_kept_node_id": content.first_kept_node_id,
        }

    @staticmethod
    def _summary_text(message: AssistantMessage) -> str:
        return "\n".join(
            block.text
            for block in message.content
            if isinstance(block, TextBlock) and block.text
        ).strip()

    def _record_failure(self, failure: ModelCallSendFailure) -> None:
        error = classify_provider_error(failure.cause)
        if isinstance(failure.cause, ProviderStreamIncompleteError):
            now = self._now()
            response = ModelCallResponse(
                assistant_message=failure.cause.assistant_message,
                provider_response=failure.cause.provider_response,
                started_at=failure.call.started_at or now,
                first_chunk_at=failure.first_chunk_at,
                finished_at=now,
                http_status=failure.cause.http_status,
            )
            ref = self._model_calls.save_response_content(response, partial=True)
            self._model_calls.mark_incomplete(
                failure.call,
                first_chunk_at=failure.first_chunk_at,
                response_content_ref=ref,
            )
            return
        self._model_calls.mark_failed(
            failure.call,
            error,
            first_chunk_at=failure.first_chunk_at,
        )
