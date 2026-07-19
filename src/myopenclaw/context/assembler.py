"""ContextAssembler：唯一 ModelContext 组装路径。

无状态；禁止依赖 Repository / Provider / Hooks / OpenViking / CLI / Coordinator。
system 与 tools 由调用方构造后传入；ToolDefinition 与 ToolSpec 类型分离。
"""

from __future__ import annotations

from myopenclaw.context.hook_feedback import HookFeedback
from myopenclaw.context.model_context import ModelContext, SystemContent, ToolDefinition
from myopenclaw.context.projection import project_messages
from myopenclaw.context.window import apply_window
from myopenclaw.conversations.agent_message import AgentMessage, UserMessage
from myopenclaw.conversations.content_blocks import TextContent
from myopenclaw.conversations.session_entry import SessionEntry


def append_hook_feedback(
    messages: list[AgentMessage],
    hook_feedback: list[HookFeedback],
) -> list[AgentMessage]:
    """将本 step 新增的 HookFeedback 合成为尾部 user 文本（不落库）。"""
    texts = [item.text for item in hook_feedback if item.text]
    if not texts:
        return list(messages)
    combined = "\n\n".join(texts)
    return [*messages, UserMessage(content=[TextContent(text=combined)])]


class ContextAssembler:
    """从 active path + system + tools + hook_feedback 组装 ModelContext。"""

    def assemble(
        self,
        *,
        entries: list[SessionEntry],
        system: SystemContent,
        tools: list[ToolDefinition],
        hook_feedback: list[HookFeedback] | None = None,
        unit_window: int = 5,
    ) -> ModelContext:
        messages = project_messages(entries)
        messages = apply_window(messages, unit_window=unit_window)
        # 调用方只传「本 step 新增」的 hook_feedback，避免多 step 累积重复注入
        messages = append_hook_feedback(messages, hook_feedback or [])
        return ModelContext(system=system, messages=messages, tools=list(tools))
