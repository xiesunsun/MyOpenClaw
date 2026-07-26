"""UsageAnchor：从 Session 派生的真实 usage 锚（设计 §6.1）。

measure 优先用「上一次真实调用的 usage」当 total 的锚，避免每次观测都远程 count。
锚只从 Session active_path 派生，不缓存进任何进程状态（§11.8）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from pickel.context.model_context import ModelContext
from pickel.conversations.agent_message import (
    AgentMessage,
    AssistantMessage,
    agent_message_from_dict,
)
from pickel.conversations.session_entry import (
    ENTRY_TYPE_COMPACTION,
    ENTRY_TYPE_MESSAGE,
    SessionEntry,
)


@dataclass(frozen=True)
class UsageAnchor:
    """上一次真实调用的用量锚。"""

    # 上次请求的实际输入规模：input + cache_read + cache_write（§5.1）
    input_tokens: int
    # 上次请求的输出；它会成为下一次请求输入的一部分
    output_tokens: int
    # 锚之后产生的新消息（不含锚 assistant 自身）
    trailing_messages: list[AgentMessage] = field(default_factory=list)

    @property
    def next_request_base(self) -> int:
        """下一次请求中「已由真实 usage 覆盖」的部分。

        上次输入 + 上次输出。漏掉 output 会让每一轮都系统性低估一个回复的体量。
        """
        return self.input_tokens + self.output_tokens


def context_fingerprint(request: ModelContext, *, provider: str, model: str) -> str:
    """provider/model + system 文本 + tools 集合的指纹。

    messages 不参与（由 trailing_messages 处理）。切 model 后 tokenizer 与计费口径
    都会变，故 provider/model 一并纳入，锚必然作废。
    """
    payload = {
        "provider": provider,
        "model": model,
        "system": request.system.as_text(),
        "tools": sorted(
            json.dumps(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            for tool in request.tools
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def resolve_anchor(
    *,
    session: Any,
    request: ModelContext,
    provider: str,
    model: str,
) -> UsageAnchor | None:
    """取 active_path 上最近一条可用的 assistant usage 作为锚。

    返回 None（锚失效）的情形：无 assistant / 最近一条模型返回无 usage /
    其后有 compaction / provider、model、system 或 tools 已变 / 旧 entry 无
    fingerprint。本地合成的 assistant（metadata 为 None）不算模型返回，按
    trailing 消息跳过。
    """
    path = session.active_path()
    if not path:
        return None

    expected = context_fingerprint(request, provider=provider, model=model)
    trailing: list[AgentMessage] = []

    for index in range(len(path) - 1, -1, -1):
        entry = path[index]
        if entry.entry_type == ENTRY_TYPE_COMPACTION:
            # 锚之后发生过压缩 → 上次 usage 与当前上下文不再对应
            return None
        if entry.entry_type != ENTRY_TYPE_MESSAGE:
            continue

        message = _message_from_entry(entry)
        if message is None:
            continue
        if not isinstance(message, AssistantMessage) or message.metadata is None:
            # 无 metadata 的 assistant 从来不是模型返回（真实回复一律经
            # ReAct._ensure_metadata 补齐 metadata），只可能是本地合成的文本，
            # 例如 max-steps 的「Reached the maximum...」。它既不携带 usage，
            # 也不代表 provider/model/system/tools 发生过变化，故按普通 trailing
            # 消息估计并继续向前找真正的锚——否则一次 max-steps 就会让锚永久失效，
            # /context 每次都退回远程 count。
            trailing.insert(0, message)
            continue

        anchor = _anchor_from_assistant(message, expected_fingerprint=expected)
        if anchor is None:
            # 该 assistant 不可用作锚（无 usage / 指纹不符）→ 整体失效，
            # 不再向前找：更早的锚同样覆盖不了这条 assistant 之后的上下文变化。
            return None
        return UsageAnchor(
            input_tokens=anchor[0],
            output_tokens=anchor[1],
            trailing_messages=trailing,
        )

    return None


def _anchor_from_assistant(
    message: AssistantMessage,
    *,
    expected_fingerprint: str,
) -> tuple[int, int] | None:
    metadata = message.metadata
    if metadata is None or metadata.usage is None:
        return None
    if metadata.context_fingerprint != expected_fingerprint:
        return None

    usage = metadata.usage
    parts = (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens)
    if all(part is None for part in parts):
        return None
    return sum(part or 0 for part in parts), usage.output_tokens or 0


def _message_from_entry(entry: SessionEntry) -> AgentMessage | None:
    try:
        return agent_message_from_dict(entry.payload)
    except (KeyError, TypeError, ValueError):
        return None
