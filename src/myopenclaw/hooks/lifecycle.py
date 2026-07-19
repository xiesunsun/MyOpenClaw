"""LifecycleHooks 分发与合并。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from myopenclaw.hooks.decisions import (
    PostToolBatchDecision,
    PostToolUseDecision,
    PreToolUseDecision,
    TurnEndDecision,
    UserPromptSubmitDecision,
    merge_feedback_texts,
    merge_pre_tool_decisions,
    merge_user_prompt_decisions,
)
from myopenclaw.hooks.events import (
    PostToolBatchEvent,
    PostToolUseEvent,
    PreToolUseEvent,
    TurnEndEvent,
    UserPromptSubmitEvent,
)


class HookHandler(Protocol):
    async def user_prompt_submit(
        self, event: UserPromptSubmitEvent
    ) -> UserPromptSubmitDecision | None: ...
    async def pre_tool_use(self, event: PreToolUseEvent) -> PreToolUseDecision | None: ...
    async def post_tool_use(self, event: PostToolUseEvent) -> PostToolUseDecision | None: ...
    async def post_tool_batch(
        self, event: PostToolBatchEvent
    ) -> PostToolBatchDecision | None: ...
    async def turn_end(self, event: TurnEndEvent) -> TurnEndDecision | None: ...


async def _call(handler: Any, method: str, event: Any) -> Any:
    fn = getattr(handler, method, None)
    if fn is None:
        return None
    try:
        result = fn(event)
        if hasattr(result, "__await__"):
            result = await result
        return result
    except Exception:
        # observer / handler 失败 best-effort
        return None


class LifecycleHooks:
    def __init__(self, handlers: list[Any] | None = None) -> None:
        self.handlers = list(handlers or [])

    async def user_prompt_submit(
        self, event: UserPromptSubmitEvent
    ) -> UserPromptSubmitDecision:
        decisions: list[UserPromptSubmitDecision] = []
        for handler in self.handlers:
            result = await _call(handler, "user_prompt_submit", event)
            if isinstance(result, UserPromptSubmitDecision):
                decisions.append(result)
        return merge_user_prompt_decisions(decisions)

    async def pre_tool_use(self, event: PreToolUseEvent) -> PreToolUseDecision:
        decisions: list[PreToolUseDecision] = []
        for handler in self.handlers:
            result = await _call(handler, "pre_tool_use", event)
            if isinstance(result, PreToolUseDecision):
                decisions.append(result)
        return merge_pre_tool_decisions(decisions)

    async def post_tool_use(self, event: PostToolUseEvent) -> PostToolUseDecision:
        texts: list[str | None] = []
        for handler in self.handlers:
            result = await _call(handler, "post_tool_use", event)
            if isinstance(result, PostToolUseDecision):
                texts.append(result.feedback_text)
        return PostToolUseDecision(feedback_text=merge_feedback_texts(texts))

    async def post_tool_batch(self, event: PostToolBatchEvent) -> PostToolBatchDecision:
        texts: list[str | None] = []
        for handler in self.handlers:
            result = await _call(handler, "post_tool_batch", event)
            if isinstance(result, PostToolBatchDecision):
                texts.append(result.feedback_text)
        return PostToolBatchDecision(feedback_text=merge_feedback_texts(texts))

    async def turn_end(self, event: TurnEndEvent) -> TurnEndDecision:
        for handler in self.handlers:
            await _call(handler, "turn_end", event)
        return TurnEndDecision()


class NoopLifecycleHooks(LifecycleHooks):
    def __init__(self) -> None:
        super().__init__(handlers=[])
