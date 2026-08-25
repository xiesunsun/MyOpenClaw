"""LifecycleHooks 分发与合并。"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any, Protocol

from pickel.context.model_context_builder import ContextContributions
from pickel.hooks.decisions import (
    PostToolBatchDecision,
    PostToolUseDecision,
    PreToolUseDecision,
    AgentRunEndDecision,
    UserPromptSubmitDecision,
    merge_feedback_texts,
    merge_pre_tool_decisions,
    merge_user_prompt_decisions,
)
from pickel.hooks.events import (
    BeforeRequestEvent,
    PostToolBatchEvent,
    PostToolUseEvent,
    PreToolUseEvent,
    AgentRunEndEvent,
    UserPromptSubmitEvent,
)
from pickel.observe.records import (
    DiagnosticRecord,
    ErrorInfo,
    SpanTimer,
    record_diagnostic,
)

logger = logging.getLogger(__name__)


class HookHandler(Protocol):
    async def user_prompt_submit(
        self, event: UserPromptSubmitEvent
    ) -> UserPromptSubmitDecision | None: ...
    async def pre_tool_use(
        self, event: PreToolUseEvent
    ) -> PreToolUseDecision | None: ...
    async def post_tool_use(
        self, event: PostToolUseEvent
    ) -> PostToolUseDecision | None: ...
    async def post_tool_batch(
        self, event: PostToolBatchEvent
    ) -> PostToolBatchDecision | None: ...
    async def before_request(
        self, event: BeforeRequestEvent
    ) -> ContextContributions | None: ...
    async def agent_run_end(
        self, event: AgentRunEndEvent
    ) -> AgentRunEndDecision | None: ...


class _HookFailed:
    pass


_HOOK_FAILED = _HookFailed()


async def _call(handler: Any, method: str, event: Any) -> Any:
    fn = getattr(handler, method, None)
    if fn is None:
        return None
    identity = event.identity
    handler_name = f"{type(handler).__module__}.{type(handler).__qualname__}"
    timer = SpanTimer(
        f"pickel.hook.{method}",
        identity,
        attributes={"handler": handler_name, "phase": method},
    )
    try:
        result = fn(event)
        if hasattr(result, "__await__"):
            result = await result
        timer.finish()
        return result
    except Exception as exc:  # noqa: BLE001 — 按 Hook 阶段决定 fail-open/closed
        logger.exception("Hook 执行异常，已按阶段策略隔离: %s.%s", handler_name, method)
        error = ErrorInfo.from_exception(exc, kind="hook")
        timer.finish(status="error", error=error)
        record_diagnostic(
            DiagnosticRecord(
                name="hook_error",
                identity=identity,
                attributes={"handler": handler_name, "phase": method},
                error=error,
            )
        )
        return _HOOK_FAILED


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
        effective_arguments = dict(event.arguments)
        for handler in self.handlers:
            current_event = replace(event, arguments=dict(effective_arguments))
            result = await _call(handler, "pre_tool_use", current_event)
            if result is _HOOK_FAILED:
                return PreToolUseDecision(
                    action="deny",
                    updated_arguments=effective_arguments,
                    reason="pre_tool_use Hook 执行异常，已安全拒绝工具调用",
                )
            if isinstance(result, PreToolUseDecision):
                decisions.append(result)
                if result.updated_arguments is not None:
                    effective_arguments = dict(result.updated_arguments)
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

    async def before_request(self, event: BeforeRequestEvent) -> ContextContributions:
        """按 Handler 注册顺序合并受限追加内容；Hook 异常保持 fail-open。"""
        system_sections = []
        messages = []
        for handler in self.handlers:
            result = await _call(handler, "before_request", event)
            if isinstance(result, ContextContributions):
                system_sections.extend(result.system_sections)
                messages.extend(result.messages)
        return ContextContributions(
            system_sections=tuple(system_sections),
            messages=tuple(messages),
        )

    async def agent_run_end(self, event: AgentRunEndEvent) -> AgentRunEndDecision:
        for handler in self.handlers:
            await _call(handler, "agent_run_end", event)
        return AgentRunEndDecision()


class NoopLifecycleHooks(LifecycleHooks):
    def __init__(self) -> None:
        super().__init__(handlers=[])
