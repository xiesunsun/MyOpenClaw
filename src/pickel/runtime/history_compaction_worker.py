"""ModelBacked 历史压缩：用冻结 Package 的 worker model 生成摘要节点。

只做五件事：选择压缩边界、保证 Tool 配对、渲染摘要输入、解析摘要输出、
提取文件账本；worker 调用的记账与重试由调用方注入的 SummarizerSender
负责，本模块不触达 ModelCallService 或 SendGate。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace

from pickel.context.history_compaction import (
    HistoryCompactionError,
    SummarizerSender,
)
from pickel.context.model_context import ModelContext, SystemContent, SystemSection
from pickel.context.token_preflight import TokenPreflightResult
from pickel.conversations.agent_message import (
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
    agent_message_to_dict,
)
from pickel.conversations.content_blocks import TextBlock, ToolCallBlock
from pickel.conversations.conversation_node import ConversationNode, HistoryCompaction

_COMPACT_SYSTEM_TEXT = (
    "你是代码 Agent 的历史压缩 worker。把用户消息中的历史对话压缩为"
    "结构化检查点，让另一个模型能无损接续工作。"
    "只输出检查点文本，不调用工具、不臆测。\n\n"
    "严格按以下 Markdown 结构输出；每一节都保留，空节写「（无）」，不得删节。"
    "用简短要点，不写散文。\n\n"
    "## 目标与意图\n"
    "## 关键决策            ← 格式：**[决策]**：[理由]\n"
    "## 已验证的命令与结果\n"
    "## 文件与代码          ← 精确路径：为什么重要 / 关键改动\n"
    "## 错误与修复\n"
    "## 未完成事项\n"
    "## 当前进展\n"
    "## 下一步              ← 单个动作，与最近请求直接对应\n"
    "## 关键上下文          ← 约束、用户偏好、环境事实、开放问题\n\n"
    "规则：\n"
    "- 保留精确的文件路径、命令、错误原文、标识符、数值、函数签名。\n"
    "- 忠实记录用户的纠正与显式指令。\n"
    "- 不提及本次压缩或上下文被压缩这一事实。\n"
    "- 若历史中已存在压缩检查点，将其合并去重：保留仍成立的事实，丢弃过时内容。"
)

# 摘要输入端的确定性截断：只影响渲染给 worker 的文本，不改写历史节点。
_RESULT_TEXT_LIMIT = 2000
_RESULT_HEAD_CHARS = 1200
_RESULT_TAIL_CHARS = 600
_RESULT_MARKER = "\n[... 中间内容已截断 ...]\n"

# 文件账本只从内置读写工具的确定性提取；bash 等自由工具不参与。
_READ_TOOL_NAMES = frozenset({"read"})
_WRITE_TOOL_NAMES = frozenset({"edit", "write"})


class ModelBackedHistoryCompactionGenerator:
    """使用冻结 Package 的 worker model 生成历史压缩节点。"""

    def __init__(
        self,
        *,
        summary_input_tokens: int = 64_000,
    ) -> None:
        if summary_input_tokens < 1:
            raise ValueError("压缩 token 预算必须大于 0")
        self._summary_input_tokens = summary_input_tokens

    async def generate(
        self,
        *,
        nodes: Sequence[ConversationNode],
        model_context: ModelContext,
        preflight: TokenPreflightResult,
        send_summarizer: SummarizerSender,
        max_summary_tokens: int,
        preserve_tail_tokens: int,
    ) -> HistoryCompaction:
        if max_summary_tokens < 1 or preserve_tail_tokens < 1:
            raise ValueError("压缩 token 预算必须大于 0")
        # model_context 与 preflight 留待热前缀复用批次使用；当前实现
        # 只依赖投影节点本身。
        del model_context, preflight
        first_kept_index = self._first_kept_index(nodes, preserve_tail_tokens)
        if first_kept_index is None:
            raise HistoryCompactionError(
                "history_compaction_no_history",
                "没有足够的历史消息可以压缩",
            )
        first_kept = nodes[first_kept_index]
        old_nodes = nodes[:first_kept_index]
        prompt = self._summary_prompt(old_nodes)
        context = ModelContext(
            system=SystemContent(
                sections=(
                    SystemSection(
                        name="history_compaction",
                        text=_COMPACT_SYSTEM_TEXT,
                    ),
                )
            ),
            messages=(UserMessage((TextBlock(prompt),)),),
            tools=(),
        )
        message = await send_summarizer(context=context, purpose="history_compaction")
        summary = self._summary_text(message)
        if not summary:
            raise HistoryCompactionError(
                "history_compaction_empty",
                "worker 压缩响应为空",
            )
        # 以下两个校验复用选材的 chars/4 估算口径，仅用于压缩有效性判断，
        # 不是 token preflight 的正式口径。
        summary_cost = max(1, len(summary) // 4)
        if summary_cost > max_summary_tokens:
            raise HistoryCompactionError(
                "history_compaction_summary_too_long",
                f"摘要估算 {summary_cost} token 超过预算 {max_summary_tokens}",
            )
        shadowed_cost = sum(self._node_cost(node) for node in old_nodes)
        if shadowed_cost > 0 and summary_cost >= shadowed_cost:
            raise HistoryCompactionError(
                "history_compaction_no_shrink",
                f"摘要估算 {summary_cost} token 未小于被压缩区域 "
                f"{shadowed_cost} token，压缩无效",
            )
        read_files, modified_files = self._extract_file_ledger(old_nodes)
        return HistoryCompaction(
            summary=summary,
            first_kept_node_id=first_kept.node_id,
            read_files=read_files,
            modified_files=modified_files,
        )

    def _first_kept_index(
        self, nodes: Sequence[ConversationNode], preserve_tail_tokens: int
    ) -> int | None:
        if len(nodes) < 2:
            return None
        total = 0
        first = len(nodes) - 1
        for index in range(len(nodes) - 1, -1, -1):
            node = nodes[index]
            if node.content_type != "agent_message":
                continue
            cost = self._node_cost(node)
            if index < len(nodes) - 1 and total + cost > preserve_tail_tokens:
                break
            total += cost
            first = index
        # 配对硬规则：保留区里的每个 ToolResult 必须能找到它的 ToolCall，
        # 否则切点会制造"有结果无调用"的孤立消息；向下修正切点直到平衡。
        first = self._repair_tool_pairing(nodes, first)
        # 活动分支不能从孤立的 Assistant 开始，否则模型会看到没有对应问题的
        # 答案。优先把紧邻的 User 一并保留；该调整只扩大保留区，不会破坏配对。
        if (
            first < len(nodes)
            and isinstance(nodes[first].content, AssistantMessage)
            and first > 0
            and isinstance(nodes[first - 1].content, UserMessage)
        ):
            first -= 1
        return first if first > 0 else None

    @staticmethod
    def _node_cost(node: ConversationNode) -> int:
        """单节点成本估算：json 字节数除以 4，只用于压缩内部决策。"""
        if node.content_type == "agent_message":
            return max(1, len(json.dumps(agent_message_to_dict(node.content))) // 4)
        return max(
            1,
            len(json.dumps(ModelBackedHistoryCompactionGenerator._node_payload(node)))
            // 4,
        )

    def _repair_tool_pairing(
        self, nodes: Sequence[ConversationNode], first: int
    ) -> int:
        """把切点下修到 tool 配对平衡处；call 丢失的孤儿结果按可修复处理。"""
        call_node_index: dict[str, int] = {}
        for index, node in enumerate(nodes):
            if node.content_type != "agent_message":
                continue
            content = node.content
            if not isinstance(content, AssistantMessage):
                continue
            for block in content.content:
                if isinstance(block, ToolCallBlock):
                    call_node_index.setdefault(block.id, index)
        while True:
            repair_to: int | None = None
            for node in nodes[first:]:
                if node.content_type != "agent_message":
                    continue
                content = node.content
                if not isinstance(content, ToolResultMessage):
                    continue
                call_index = call_node_index.get(content.tool_call_id)
                if call_index is None or call_index >= first:
                    continue
                repair_to = (
                    call_index if repair_to is None else min(repair_to, call_index)
                )
            if repair_to is None:
                return first
            first = repair_to

    def _summary_prompt(self, nodes: Sequence[ConversationNode]) -> str:
        rendered = []
        for index, node in enumerate(nodes):
            rendered.append(
                f"[{index}] {json.dumps(self._node_payload(node), ensure_ascii=False)}"
            )
        text = "\n\n".join(rendered)
        limit = self._summary_input_tokens * 4
        if len(text) <= limit:
            return "请压缩以下历史消息：\n\n" + text
        head = limit // 2
        tail = limit - head
        return (
            "请压缩以下历史消息。中间部分因压缩 worker 输入预算被省略，"
            "不要假设省略部分的具体内容：\n\n"
            + text[:head]
            + "\n\n[中间历史已省略]\n\n"
            + text[-tail:]
        )

    @staticmethod
    def _node_payload(node: ConversationNode) -> dict:
        if node.content_type == "agent_message":
            content = node.content
            if isinstance(content, ToolResultMessage):
                content = _truncate_result_text(content)
            return agent_message_to_dict(content)
        compaction = node.content
        payload: dict = {
            "content_type": "history_compaction",
            "summary": compaction.summary,
            "first_kept_node_id": compaction.first_kept_node_id,
        }
        if compaction.read_files:
            payload["read_files"] = list(compaction.read_files)
        if compaction.modified_files:
            payload["modified_files"] = list(compaction.modified_files)
        return payload

    @staticmethod
    def _extract_file_ledger(
        nodes: Sequence[ConversationNode],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """从被压缩区域提取文件账本：内置读写工具 + 前序压缩账本累积。"""
        reads: set[str] = set()
        writes: set[str] = set()
        for node in nodes:
            if node.content_type == "history_compaction":
                compaction = node.content
                reads.update(compaction.read_files)
                writes.update(compaction.modified_files)
                continue
            if node.content_type != "agent_message":
                continue
            content = node.content
            if not isinstance(content, AssistantMessage):
                continue
            for block in content.content:
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


def _truncate_result_text(message: ToolResultMessage) -> ToolResultMessage:
    """截断超限的 tool result 文本块；只影响摘要输入渲染，不改写历史。"""
    blocks = message.content
    if not any(
        isinstance(block, TextBlock) and len(block.text) > _RESULT_TEXT_LIMIT
        for block in blocks
    ):
        return message
    truncated = []
    for block in blocks:
        if isinstance(block, TextBlock) and len(block.text) > _RESULT_TEXT_LIMIT:
            head = block.text[:_RESULT_HEAD_CHARS]
            tail = block.text[-_RESULT_TAIL_CHARS:]
            truncated.append(TextBlock(f"{head}{_RESULT_MARKER}{tail}"))
        else:
            truncated.append(block)
    return replace(message, content=tuple(truncated))
