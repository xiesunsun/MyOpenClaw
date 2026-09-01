"""Delegation 终态结果的紧凑、纯投影。

该模块只把已经持久化的终态事实映射成模型可见的 JSON 值，不读取 Store，也
不包含 Session、Operation 或 ConversationNode 的身份字段。settled 消息和
``wait_delegation`` 必须共用这份投影，避免两条路径逐渐产生不同的结果合同。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from pickel.conversations.agent_message import AssistantMessage, UserMessage
from pickel.conversations.content_blocks import (
    ArtifactBlock,
    TextBlock,
    content_block_from_dict,
    content_block_to_dict,
)
from pickel.operations.agent_run_state import (
    AgentRunError,
    Cancellation,
)

DEFAULT_DELEGATION_RESULT_MAX_CHARS = 8000


@dataclass(frozen=True)
class DelegationResultProjector:
    """将 Child Operation 终态投影为稳定的 ``result``/``error``。

    ``max_chars`` 只限制文本块的总字符数；ArtifactBlock 不占用预算，也不会
    被截断。投影器不修改输入消息，因而同一终态每次都会得到相同的值。
    """

    max_chars: int = DEFAULT_DELEGATION_RESULT_MAX_CHARS

    def __post_init__(self) -> None:
        if isinstance(self.max_chars, bool) or not isinstance(self.max_chars, int):
            raise TypeError("delegation result max_chars 必须是整数")
        if self.max_chars < 0:
            raise ValueError("delegation result max_chars 不能小于 0")

    def project(
        self,
        status: str,
        assistant_message: AssistantMessage | None = None,
        error: AgentRunError | None = None,
        cancellation: Cancellation | None = None,
    ) -> dict[str, object]:
        """投影一个终态或当前等待状态。

        非终态没有结果或错误，供 ``wait_delegation`` 超时响应复用。成功必须
        提供由 ``final_assistant_node_id`` 读取出的 AssistantMessage；失败与
        取消不携带模型消息，避免错误路径泄漏完整历史或异常栈。
        """
        if status == "succeeded":
            if assistant_message is None:
                raise ValueError("succeeded Delegation 缺少最终 AssistantMessage")
            content, truncated, omitted_chars = self._assistant_content(
                assistant_message
            )
            projection: dict[str, object] = {"result": content, "error": None}
            if truncated:
                # 小结果保持旧 JSON 形状；只有发生有界投影时才暴露元数据。
                projection.update({"truncated": True, "omitted_chars": omitted_chars})
            return projection
        if status == "failed":
            return {"result": None, "error": self._error(error)}
        if status == "cancelled":
            return {"result": None, "error": self._cancellation(cancellation)}

        # ``archived`` 是 ChildAgentSnapshot 的查询状态；若它仍携带最终消息，
        # wait 仍应返回同一成功投影。其余运行态只表示尚未有结果。
        if status == "archived" and assistant_message is not None:
            content, truncated, omitted_chars = self._assistant_content(
                assistant_message
            )
            projection = {"result": content, "error": None}
            if truncated:
                projection.update({"truncated": True, "omitted_chars": omitted_chars})
            return projection
        return {"result": None, "error": None}

    def _assistant_content(
        self, message: AssistantMessage
    ) -> tuple[list[dict[str, Any]], bool, int]:
        content: list[dict[str, Any]] = []
        remaining = self.max_chars
        omitted_chars = 0
        for block in message.content:
            if isinstance(block, TextBlock):
                if remaining <= 0:
                    omitted_chars += len(block.text)
                    continue
                text = block.text[:remaining]
                remaining -= len(text)
                omitted_chars += len(block.text) - len(text)
                if text:
                    content.append(content_block_to_dict(TextBlock(text=text)))
            elif isinstance(block, ArtifactBlock):
                # Artifact 是稳定的消息值引用，不受文本预算影响。
                content.append(content_block_to_dict(block))
        return content, omitted_chars > 0, omitted_chars

    @staticmethod
    def _error(error: AgentRunError | None) -> dict[str, object]:
        if error is None:
            # 终态 State 合同要求 failed 必须有 AgentRunError；保留稳定降级值
            # 以便旧快照仍能通过 wait 返回，而不暴露实现异常。
            return {
                "code": "failed",
                "message": "child agent failed",
                "retryable": False,
            }
        return {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
        }

    @staticmethod
    def _cancellation(cancellation: Cancellation | None) -> dict[str, object]:
        # requested_at 是恢复事实，不是对 Parent 有用的结果；不投影时间戳，
        # 使重复读取同一终态保持稳定且不泄漏内部执行细节。
        return {
            "code": "cancelled",
            "message": (
                cancellation.cause
                if cancellation is not None
                else "child agent cancelled"
            ),
            "retryable": False,
        }


def project_delegation_result(
    status: str,
    assistant_message: AssistantMessage | None = None,
    error: AgentRunError | None = None,
    cancellation: Cancellation | None = None,
    max_chars: int = DEFAULT_DELEGATION_RESULT_MAX_CHARS,
) -> dict[str, object]:
    """投影 Delegation 结果的函数入口，供持久化和工具路径共享。"""
    return DelegationResultProjector(max_chars=max_chars).project(
        status=status,
        assistant_message=assistant_message,
        error=error,
        cancellation=cancellation,
    )


def project_settled_message(
    *,
    child_session_id: str,
    status: str,
    assistant_message: AssistantMessage | None = None,
    error: AgentRunError | None = None,
    cancellation: Cancellation | None = None,
    max_chars: int = DEFAULT_DELEGATION_RESULT_MAX_CHARS,
) -> UserMessage:
    """把已确认的 child 终态投影为 Parent 可理解的 UserMessage。

    第一个 TextBlock 是稳定 envelope；后续内容只来自共享的 delegation
    result projection，不携带 Operation、Node、Provider 或 usage 身份。
    """
    projection = project_delegation_result(
        status=status,
        assistant_message=assistant_message,
        error=error,
        cancellation=cancellation,
        max_chars=max_chars,
    )
    truncated = bool(projection.get("truncated", False))
    envelope = TextBlock(
        json.dumps(
            {
                "child_session_id": child_session_id,
                "status": status,
                "type": "agent_settled",
                **(
                    {
                        "truncated": True,
                        "omitted_chars": projection["omitted_chars"],
                    }
                    if truncated
                    else {}
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    result = projection["result"]
    if result is not None:
        return UserMessage(
            (envelope, *(content_block_from_dict(item) for item in result))  # type: ignore[arg-type]
        )
    return UserMessage(
        (
            envelope,
            TextBlock(
                json.dumps(
                    {"error": projection["error"]},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            ),
        )
    )


def delegation_result_max_chars(parent_package_or_policy: object | None) -> int:
    """读取 Parent Package 的结果预算，兼容缺失字段的旧 Package。

    Package format 1/2 尚未保存该字段时固定使用 8000。该 helper 不修改 Package
    模型，供后续 Package v3 终态投递路径直接使用。
    """
    if parent_package_or_policy is None:
        return DEFAULT_DELEGATION_RESULT_MAX_CHARS
    policy = getattr(
        parent_package_or_policy, "runtime_policy", parent_package_or_policy
    )
    value = getattr(policy, "delegation_result_max_chars", None)
    if value is None:
        return DEFAULT_DELEGATION_RESULT_MAX_CHARS
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("delegation_result_max_chars 必须是大于等于 0 的整数")
    return value
