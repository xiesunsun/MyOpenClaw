"""ConversationSession 自动标题的窄服务。

标题不是 AgentRun：它只使用首个已接受 Operation 冻结的 Package 中的
utility Provider，并通过 ModelCallService/ModelCallSendGate 记账。标题提交
本身使用 Session 空标题 CAS，因此并发标题任务和用户改名都安全。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from pickel.agents.agent_package import LoadedAgentPackage
from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_store import ConversationStore
from pickel.context.model_context import ModelContext, SystemContent
from pickel.model_calls.service import ModelCallService
from pickel.runtime.model_call_send_gate import ModelCallSendFailure, ModelCallSendGate
from pickel.runtime.runtime_effects import RuntimeEffects
from pickel.operations.session_operation import SessionOperation


@dataclass(frozen=True)
class TitleGenerationResult:
    """一次标题任务的可测试结果；不代表 Session 一定成功更新。"""

    title: str
    committed: bool
    used_fallback: bool
    attempts: int


class ConversationTitleService:
    """异步生成并 CAS 提交 ConversationSession 标题。"""

    def __init__(
        self,
        *,
        store: ConversationStore,
        model_calls: ModelCallService | None = None,
        send_gate: ModelCallSendGate | None = None,
        now: Callable[[], datetime] | None = None,
        max_chars: int = 80,
        max_attempts: int = 2,
    ) -> None:
        if max_chars < 1:
            raise ValueError("max_chars 必须大于 0")
        if max_attempts < 1:
            raise ValueError("max_attempts 必须大于 0")
        self._store = store
        self._model_calls = model_calls or ModelCallService(store)  # type: ignore[arg-type]
        self._send_gate = send_gate or ModelCallSendGate(store)  # type: ignore[arg-type]
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._max_chars = max_chars
        self._max_attempts = max_attempts

    async def generate(
        self,
        *,
        operation: SessionOperation,
        loaded_agent_package: LoadedAgentPackage,
    ) -> TitleGenerationResult:
        """为 operation 的首条 UserMessage 生成标题并尝试一次 CAS。"""
        session = self._store.load_session(operation.session_id)
        if session is None:
            raise LookupError(f"ConversationSession 不存在: {operation.session_id}")
        if session.title is not None or session.archived_at is not None:
            return TitleGenerationResult(
                title=session.title or "",
                committed=False,
                used_fallback=False,
                attempts=0,
            )

        node = self._store.load_node(operation.input_node_id)
        message = node.content if node is not None else None
        if not isinstance(message, UserMessage):
            # 接受事务应当总是以 UserMessage 作为 input node；损坏数据不触发
            # Provider，但仍用确定性的本地标题收敛，避免任务永久悬挂。
            fallback = "新对话"
        else:
            fallback = self._truncate(_user_text(message)) or "新对话"

        utility = loaded_agent_package.version.model_policy.utility
        provider = loaded_agent_package.model_clients.get("utility")
        if (
            loaded_agent_package.version.package_version_id
            != operation.agent_package_version_id
        ):
            # 不能拿当前 Agent 外壳的同名/新版本 Package 冒充 Operation
            # 冻结版本；调用方应改为本地 fallback 或稍后重新加载。
            provider = None
        if utility is None or provider is None or not isinstance(message, UserMessage):
            return await self._commit(
                session_id=operation.session_id,
                title=fallback,
                used_fallback=True,
                attempts=0,
            )

        context = ModelContext(
            system=SystemContent.from_text(
                "为用户消息生成简短对话标题。只返回标题文本，不要引号、编号或解释。"
            ),
            messages=(message,),
        )
        title = ""
        attempts = 0
        for attempts in range(1, self._max_attempts + 1):
            try:
                prepared_call = await self._model_calls.prepare_session_call_async(
                    session_id=operation.session_id,
                    context=context,
                    mapper=provider,
                    request_attempt=attempts,
                    model_role="utility",
                    purpose="title",
                )
                effects = RuntimeEffects(
                    provider=provider,
                    provider_name=utility.provider,
                    model_name=utility.model,
                    model_request_limiter=getattr(
                        loaded_agent_package, "model_request_limiter", None
                    ),
                )
                response = await self._send_gate.send(
                    call=prepared_call.model_call,
                    prepared=prepared_call.prepared,
                    effects=effects,
                )
                await self._model_calls.complete_session_response_async(
                    call=prepared_call.model_call, response=response
                )
                title = self._truncate(_assistant_text(response.assistant_message))
                if title:
                    break
            except asyncio.CancelledError:
                raise
            except ModelCallSendFailure as exc:
                try:
                    await self._model_calls.record_send_failure_async(
                        exc.call, exc.cause, first_chunk_at=exc.first_chunk_at
                    )
                except Exception:
                    # 失败记账本身不能阻止本地标题兜底；下次加载仍可重试。
                    pass
            except Exception:
                # Prepare/complete 的存储冲突或 Provider 映射异常都属于标题
                # 辅助路径失败，不得影响主回答。
                pass
        return await self._commit(
            session_id=operation.session_id,
            title=title or fallback,
            used_fallback=not bool(title),
            attempts=attempts,
        )

    async def _commit(
        self, *, session_id: str, title: str, used_fallback: bool, attempts: int
    ) -> TitleGenerationResult:
        committed = await asyncio.to_thread(
            self._store.commit_generated_title,
            session_id=session_id,
            title=title,
            updated_at=self._now(),
        )
        return TitleGenerationResult(
            title=title,
            committed=committed,
            used_fallback=used_fallback,
            attempts=attempts,
        )

    def _truncate(self, value: str) -> str:
        return " ".join(value.split())[: self._max_chars].strip()


def _user_text(message: UserMessage) -> str:
    return "\n".join(
        block.text for block in message.content if isinstance(block, TextBlock)
    )


def _assistant_text(message: AssistantMessage) -> str:
    return "\n".join(
        block.text for block in message.content if isinstance(block, TextBlock)
    )
