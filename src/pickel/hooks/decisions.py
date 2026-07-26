"""Hook 决策 DTO 与合并规则。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pickel.context.model_context import ModelContext


@dataclass
class UserPromptSubmitDecision:
    action: Literal["continue", "block"] = "continue"
    feedback_text: str | None = None
    reason: str | None = None


@dataclass
class PreToolUseDecision:
    action: Literal["allow", "deny", "ask"] = "allow"
    updated_arguments: dict[str, Any] | None = None
    reason: str | None = None
    feedback_text: str | None = None


@dataclass
class PostToolUseDecision:
    feedback_text: str | None = None


@dataclass
class PostToolBatchDecision:
    feedback_text: str | None = None


@dataclass
class TurnEndDecision:
    """观察者；无控制动作。"""
    pass


@dataclass
class BeforeRequestDecision:
    """可选替换拟发送 ModelContext；feedback 文本可合并。"""

    model_context: ModelContext | None = None
    feedback_text: str | None = None


def merge_user_prompt_decisions(
    decisions: list[UserPromptSubmitDecision],
) -> UserPromptSubmitDecision:
    """任一 block → 整体 block；反馈文本拼接。"""
    if not decisions:
        return UserPromptSubmitDecision()
    blocked = [d for d in decisions if d.action == "block"]
    feedbacks = [d.feedback_text for d in decisions if d.feedback_text]
    feedback = "\n".join(feedbacks) if feedbacks else None
    if blocked:
        reasons = [d.reason for d in blocked if d.reason]
        return UserPromptSubmitDecision(
            action="block",
            feedback_text=feedback,
            reason=reasons[0] if reasons else "blocked",
        )
    return UserPromptSubmitDecision(action="continue", feedback_text=feedback)


def merge_pre_tool_decisions(
    decisions: list[PreToolUseDecision],
) -> PreToolUseDecision:
    """deny > ask > allow；updated_arguments 按顺序叠加。"""
    if not decisions:
        return PreToolUseDecision()
    action: Literal["allow", "deny", "ask"] = "allow"
    for d in decisions:
        if d.action == "deny":
            action = "deny"
            break
        if d.action == "ask" and action != "deny":
            action = "ask"
    args: dict[str, Any] | None = None
    for d in decisions:
        if d.updated_arguments is not None:
            args = dict(d.updated_arguments)
    reasons = [d.reason for d in decisions if d.reason]
    feedbacks = [d.feedback_text for d in decisions if d.feedback_text]
    # v1: ask 按 deny 处理
    if action == "ask":
        return PreToolUseDecision(
            action="deny",
            updated_arguments=args,
            reason=reasons[0] if reasons else "需要确认（第一版未接 UI）",
            feedback_text="\n".join(feedbacks) if feedbacks else None,
        )
    return PreToolUseDecision(
        action=action,
        updated_arguments=args,
        reason=reasons[0] if reasons else None,
        feedback_text="\n".join(feedbacks) if feedbacks else None,
    )


def merge_feedback_texts(texts: list[str | None]) -> str | None:
    parts = [t for t in texts if t]
    return "\n".join(parts) if parts else None


def merge_before_request_decisions(
    decisions: list[BeforeRequestDecision],
) -> BeforeRequestDecision:
    """model_context：最后一个非 None 覆盖；feedback 文本拼接。"""
    if not decisions:
        return BeforeRequestDecision()
    model_context = None
    for d in decisions:
        if d.model_context is not None:
            model_context = d.model_context
    feedback = merge_feedback_texts([d.feedback_text for d in decisions])
    return BeforeRequestDecision(model_context=model_context, feedback_text=feedback)
