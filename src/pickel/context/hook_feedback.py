"""Hook 模型可见反馈（不落库）。

定义在 context 包，供 Assembler 消费；不依赖 hooks/runs 包。
source_event 仅作观测，不注入模型文本。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HookFeedback:
    """已生效、待注入 ModelContext 尾部的合成 user 反馈。"""

    source_event: str  # 如 "UserPromptSubmit" / "PostToolBatch"；仅观测
    text: str
