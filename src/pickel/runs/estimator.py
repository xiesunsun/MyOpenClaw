"""本地 token 估计：纯计算、无网络、无异步。

measure 的分栏一律走这里（设计 §6.2）。远程 count 只用于 total 的 C 档兜底，
不参与分栏，因此本模块不得依赖 providers。

chars/4 是起步启发式；换本地 tokenizer 时只改 estimate_text 一处。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

CHARS_PER_TOKEN = 4

# 图片不计字符，给一个保守的固定成本，避免整块内容被估成 0
IMAGE_TOKEN_COST = 800

# 每条消息的角色标记等结构开销
MESSAGE_OVERHEAD_TOKENS = 4


def estimate_text(text: str) -> int:
    """文本 → 估计 token 数。"""
    if not text:
        return 0
    return len(text) // CHARS_PER_TOKEN


def estimate_messages(messages: Iterable[Any]) -> int:
    """消息列表 → 估计 token 数（含 thinking / tool call / tool result）。"""
    total = 0
    for message in messages:
        total += MESSAGE_OVERHEAD_TOKENS
        total += estimate_text(getattr(message, "tool_name", "") or "")
        for block in getattr(message, "content", []) or []:
            total += _estimate_block(block)
    return total


def estimate_tools(tools: Iterable[Any]) -> int:
    """工具定义列表 → 估计 token 数。"""
    total = 0
    for tool in tools:
        total += estimate_text(getattr(tool, "name", "") or "")
        total += estimate_text(getattr(tool, "description", "") or "")
        total += estimate_text(_dump_schema(getattr(tool, "input_schema", None)))
    return total


def request_char_count(request: Any) -> int:
    """整个 Request 的字符量；用于度量 before_request 的改写幅度。

    字符而非 token：这是「hook 改了多少」的诊断量，不参与占用计算，
    不值得为它触网或估算。
    """
    total = len(request.system.as_text())
    for message in request.messages:
        for block in getattr(message, "content", []) or []:
            total += len(getattr(block, "text", "") or "")
            total += len(_dump_schema(getattr(block, "arguments", None)))
    for tool in request.tools:
        total += len(tool.name) + len(tool.description)
        total += len(_dump_schema(tool.input_schema))
    return total


def _estimate_block(block: Any) -> int:
    block_type = getattr(block, "type", None)
    if block_type == "image":
        return IMAGE_TOKEN_COST
    if block_type == "tool_call":
        return (
            estimate_text(getattr(block, "name", "") or "")
            + estimate_text(_dump_schema(getattr(block, "arguments", None)))
        )
    return estimate_text(getattr(block, "text", "") or "")


def _dump_schema(value: Any) -> str:
    if value is None:
        return ""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value)
