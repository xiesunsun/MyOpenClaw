"""TurnUsage：从 Session 派生的真实 API usage 合计（设计 §5）。

只读派生，不写 Session，不缓存进程状态（§11.8）。
一个 turn 可能有多次 generate，故按 turn 合计而非只取最后一次 step。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator

from pickel.conversations.agent_message import (
    AgentMessage,
    AssistantMessage,
    UserMessage,
    agent_message_from_dict,
)
from pickel.conversations.session_entry import ENTRY_TYPE_MESSAGE, SessionEntry


@dataclass(frozen=True)
class TurnUsage:
    """一段区间内所有模型调用的真实用量合计。"""

    steps: int = 0
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    elapsed_ms: int = 0
    hook_injected_chars: int = 0
    model_label: str | None = None

    @property
    def actual_input_tokens(self) -> int:
        """实际输入规模（§5.1）。

        Anthropic 的 input_tokens 不含 cache，单独展示会低估一个数量级。
        """
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens


def last_turn_usage(session: Any) -> TurnUsage | None:
    """最近一轮（最后一条 user 之后的全部 assistant）的用量合计。"""
    messages = list(_messages(session))
    start = 0
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], UserMessage):
            start = index + 1
            break
    return _accumulate(messages[start:])


def session_usage(session: Any) -> TurnUsage | None:
    """整个 active path 的用量合计。"""
    return _accumulate(list(_messages(session)))


def _accumulate(messages: list[AgentMessage]) -> TurnUsage | None:
    steps = 0
    totals = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
    elapsed_ms = 0
    hook_chars = 0
    model_label: str | None = None

    for message in messages:
        if not isinstance(message, AssistantMessage) or message.metadata is None:
            continue
        metadata = message.metadata
        if metadata.usage is None:
            continue
        usage = metadata.usage
        steps += 1
        totals["input"] += usage.input_tokens or 0
        totals["cache_read"] += usage.cache_read_tokens or 0
        totals["cache_write"] += usage.cache_write_tokens or 0
        totals["output"] += usage.output_tokens or 0
        elapsed_ms += metadata.elapsed_ms or 0
        hook_chars += metadata.hook_injected_chars or 0
        model_label = f"{metadata.provider} / {metadata.model}"

    if steps == 0:
        return None
    return TurnUsage(
        steps=steps,
        input_tokens=totals["input"],
        cache_read_tokens=totals["cache_read"],
        cache_write_tokens=totals["cache_write"],
        output_tokens=totals["output"],
        elapsed_ms=elapsed_ms,
        hook_injected_chars=hook_chars,
        model_label=model_label,
    )


def _messages(session: Any) -> Iterator[AgentMessage]:
    for entry in session.active_path():
        message = _message_from_entry(entry)
        if message is not None:
            yield message


def _message_from_entry(entry: SessionEntry) -> AgentMessage | None:
    if entry.entry_type != ENTRY_TYPE_MESSAGE:
        return None
    try:
        return agent_message_from_dict(entry.payload)
    except (KeyError, TypeError, ValueError):
        return None
