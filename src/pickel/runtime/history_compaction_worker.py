"""Provider-neutral HistoryCompaction worker generator。"""

from __future__ import annotations

import json
from collections.abc import Sequence

from pickel.context.history_compaction import HistoryCompactionError, SummarizerSender
from pickel.context.model_context import ModelContext, SystemContent, SystemSection
from pickel.conversations.agent_message import (
    AgentMessage,
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
    agent_message_to_dict,
)
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.conversations.conversation_node import HistoryCompaction

_COMPACT_SYSTEM_TEXT = """你是代码 Agent 的历史压缩 worker。把逻辑历史压缩为可接续工作的结构化检查点。
只输出检查点文本，不调用工具、不臆测。每一节都必须出现；空节写「（无）」。用简短要点，不写散文。

## 当前目标与用户意图
## 已确认约束与偏好
## 关键决策与理由
## 已完成工作与当前状态
## 文件与代码
## 已验证命令与结果
## 错误、失败尝试与修复
## 未完成事项与开放问题
## 下一步

保留精确路径、命令、错误原文、标识符、数值和函数签名；忠实记录用户纠正与显式指令。
固定 System、Skills、Tool Definitions、Recall/Hook 临时贡献和 Goal/Plan 临时状态不属于摘要。"""

_READ_TOOL_NAMES = frozenset({"read"})
_WRITE_TOOL_NAMES = frozenset({"edit", "write"})


class ModelBackedHistoryCompactionGenerator:
    """使用注入的 worker sender 生成自包含 HistoryCompaction 内容。"""

    async def generate(
        self,
        *,
        previous_summary: str | None,
        exact_messages: Sequence[AgentMessage],
        previous_read_files: Sequence[str] = (),
        previous_modified_files: Sequence[str] = (),
        model_context: ModelContext | None = None,
        worker_input_limit: int,
        send_summarizer: SummarizerSender,
        max_summary_tokens: int,
        preserve_tail_tokens: int,
    ) -> HistoryCompaction:
        if worker_input_limit < 1 or max_summary_tokens < 1 or preserve_tail_tokens < 1:
            raise ValueError("压缩 token 预算必须大于 0")
        # 本轮使用隔离 worker envelope；model_context 仅为后续 warm prefix
        # 优化保留，不得改变摘要范围或 checkpoint 数据合同。
        del model_context
        messages = tuple(exact_messages)
        self._validate_messages(messages)
        retained_start = self._retained_start(messages, preserve_tail_tokens)
        if retained_start <= 0:
            raise HistoryCompactionError(
                "history_compaction_no_history", "没有足够的历史消息可以压缩"
            )
        shadowed = messages[:retained_start]
        summary_input = self._summary_input(
            previous_summary=previous_summary,
            messages=shadowed,
            previous_read_files=previous_read_files,
            previous_modified_files=previous_modified_files,
        )
        prompt = self._render_summary_input(summary_input)
        if self._estimate_text_tokens(prompt) > worker_input_limit:
            raise HistoryCompactionError(
                "history_compaction_input_too_large",
                "完整摘要输入超过 worker 有效输入窗口",
            )
        context = ModelContext(
            system=SystemContent(
                sections=(SystemSection("history_compaction", _COMPACT_SYSTEM_TEXT),)
            ),
            messages=(UserMessage((TextBlock(prompt),)),),
            tools=(),
        )
        response = await send_summarizer(context=context, purpose="history_compaction")
        summary = self._summary_text(response)
        if not summary:
            raise HistoryCompactionError(
                "history_compaction_empty", "worker 压缩响应为空"
            )
        summary_cost = self._estimate_text_tokens(summary)
        if summary_cost > max_summary_tokens:
            raise HistoryCompactionError(
                "history_compaction_summary_too_long",
                f"摘要估算 {summary_cost} token 超过预算 {max_summary_tokens}",
            )
        shadowed_cost = sum(self._message_cost(message) for message in shadowed)
        if summary_cost >= shadowed_cost:
            raise HistoryCompactionError(
                "history_compaction_no_shrink",
                f"摘要估算 {summary_cost} token 未小于被压缩区域 {shadowed_cost} token",
            )
        reads, writes = self._extract_file_ledger(
            shadowed, previous_read_files, previous_modified_files
        )
        return self._new_compaction(
            summary=summary,
            retained_messages=messages[retained_start:],
            read_files=reads,
            modified_files=writes,
        )

    @staticmethod
    def _new_compaction(**values: object) -> HistoryCompaction:
        try:
            return HistoryCompaction(**values)  # type: ignore[arg-type]
        except TypeError as exc:
            raise HistoryCompactionError(
                "history_compaction_invalid_result",
                "HistoryCompaction codec 尚未提供 retained_messages 字段",
            ) from exc

    @staticmethod
    def _validate_messages(messages: Sequence[AgentMessage]) -> None:
        calls: set[str] = set()
        for message in messages:
            if isinstance(message, AssistantMessage):
                calls.update(
                    block.id
                    for block in message.content
                    if isinstance(block, ToolCallBlock)
                )
            elif (
                isinstance(message, ToolResultMessage)
                and message.tool_call_id not in calls
            ):
                raise HistoryCompactionError(
                    "history_compaction_tool_pairing",
                    f"ToolResult {message.tool_call_id} 找不到前置 ToolCall",
                )

    @classmethod
    def _retained_start(cls, messages: Sequence[AgentMessage], budget: int) -> int:
        total = 0
        start = len(messages)
        for index in range(len(messages) - 1, -1, -1):
            cost = cls._message_cost(messages[index])
            if start < len(messages) and total + cost > budget:
                break
            total += cost
            start = index
        if start == len(messages) or start == 0:
            return start
        calls: dict[str, int] = {}
        for index, message in enumerate(messages):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolCallBlock):
                        calls[block.id] = index
        while start < len(messages) and isinstance(messages[start], ToolResultMessage):
            start = calls[messages[start].tool_call_id]
        if isinstance(messages[start], AssistantMessage) and start > 0:
            if isinstance(messages[start - 1], UserMessage):
                start -= 1
        return start

    @staticmethod
    def _summary_input(
        *,
        previous_summary: str | None,
        messages: Sequence[AgentMessage],
        previous_read_files: Sequence[str],
        previous_modified_files: Sequence[str],
    ) -> tuple[object, ...]:
        items: list[object] = []
        if previous_summary:
            items.append({"type": "previous_summary", "text": previous_summary})
        items.extend(messages)
        if previous_read_files or previous_modified_files:
            items.append(
                {
                    "type": "previous_file_ledger",
                    "read_files": sorted(set(previous_read_files)),
                    "modified_files": sorted(set(previous_modified_files)),
                }
            )
        return tuple(items)

    @staticmethod
    def _render_summary_input(items: Sequence[object]) -> str:
        rendered = ["请压缩以下完整逻辑历史："]
        for index, item in enumerate(items):
            payload = item if isinstance(item, dict) else agent_message_to_dict(item)  # type: ignore[arg-type]
            rendered.append(
                f"[{index}] {json.dumps(payload, ensure_ascii=False, sort_keys=True)}"
            )
        return "\n\n".join(rendered)

    @staticmethod
    def _estimate_text_tokens(value: str) -> int:
        return max(1, (len(value) + 3) // 4)

    @classmethod
    def _message_cost(cls, message: AgentMessage) -> int:
        return cls._estimate_text_tokens(
            json.dumps(
                agent_message_to_dict(message), ensure_ascii=False, sort_keys=True
            )
        )

    @staticmethod
    def _extract_file_ledger(
        messages: Sequence[AgentMessage],
        previous_read_files: Sequence[str],
        previous_modified_files: Sequence[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        reads, writes = set(previous_read_files), set(previous_modified_files)
        for message in messages:
            if not isinstance(message, AssistantMessage):
                continue
            for block in message.content:
                if not isinstance(block, ToolCallBlock):
                    continue
                path = block.arguments.get("path")
                if not isinstance(path, str) or not path:
                    continue
                if block.name in _READ_TOOL_NAMES:
                    reads.add(path)
                elif block.name in _WRITE_TOOL_NAMES:
                    writes.add(path)
        return tuple(sorted(reads)), tuple(sorted(writes))

    @staticmethod
    def _summary_text(message: AssistantMessage) -> str:
        return "\n".join(
            block.text
            for block in message.content
            if isinstance(block, TextBlock) and block.text
        ).strip()
