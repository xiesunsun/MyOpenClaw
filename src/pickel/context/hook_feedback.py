"""Hook 模型可见反馈（不落库）。

定义在 context 包，供 ModelContextBuilder 和请求 Hook 消费；不依赖 hooks/runs 包。
source_event 仅作观测，不注入模型文本。
"""

from __future__ import annotations

from dataclasses import dataclass

from pickel.conversations.agent_message import AgentMessage, UserMessage
from pickel.conversations.content_blocks import TextBlock


@dataclass(frozen=True)
class HookFeedback:
    """已生效、待注入 ModelContext 尾部的合成 user 反馈。"""

    source_event: str  # 如 "UserPromptSubmit" / "PostToolBatch"；仅观测
    text: str


def append_hook_feedback(
    messages: list[AgentMessage],
    hook_feedback: list[HookFeedback],
) -> list[AgentMessage]:
    """将当前 ModelStep 新增的 Hook 反馈合成为尾部用户文本，不落库。"""
    texts = [item.text for item in hook_feedback if item.text]
    if not texts:
        return list(messages)
    return [
        *messages,
        UserMessage(content=[TextBlock(text="\n\n".join(texts))]),
    ]
