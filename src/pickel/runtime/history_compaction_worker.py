"""ModelBacked 历史压缩：用冻结 Package 的 worker model 生成摘要节点。

只做三件事：选择压缩边界、渲染摘要输入、解析摘要输出；worker 调用的
记账与重试由调用方注入的 SummarizerSender 负责，本模块不触达
ModelCallService 或 SendGate。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from pickel.context.history_compaction import (
    HistoryCompactionError,
    SummarizerSender,
)
from pickel.context.model_context import ModelContext, SystemContent, SystemSection
from pickel.context.token_preflight import TokenPreflightResult
from pickel.conversations.agent_message import (
    AssistantMessage,
    UserMessage,
    agent_message_to_dict,
)
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_node import ConversationNode, HistoryCompaction


class ModelBackedHistoryCompactionGenerator:
    """使用冻结 Package 的 worker model 生成历史压缩节点。"""

    def __init__(
        self,
        *,
        preserve_tail_tokens: int = 32_000,
        summary_input_tokens: int = 64_000,
    ) -> None:
        if preserve_tail_tokens < 1 or summary_input_tokens < 1:
            raise ValueError("压缩 token 预算必须大于 0")
        self._preserve_tail_tokens = preserve_tail_tokens
        self._summary_input_tokens = summary_input_tokens

    async def generate(
        self,
        *,
        nodes: Sequence[ConversationNode],
        model_context: ModelContext,
        preflight: TokenPreflightResult,
        send_summarizer: SummarizerSender,
    ) -> HistoryCompaction:
        # model_context 与 preflight 留待批次 B 使用：预检口径的收缩校验
        # 与摘要输入复用；当前实现只依赖投影节点本身。
        del model_context, preflight
        first_kept_index = self._first_kept_index(nodes)
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
                        text=(
                            "你是代码 Agent 的历史压缩 worker。只输出简洁的中文事实摘要，"
                            "不得调用工具、不得臆测。必须保留用户目标、约束、已经验证的命令与结果、"
                            "修改过的文件、错误、未完成事项和下一步。"
                        ),
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
        return HistoryCompaction(
            summary=summary,
            first_kept_node_id=first_kept.node_id,
        )

    def _first_kept_index(self, nodes: Sequence[ConversationNode]) -> int | None:
        if len(nodes) < 2:
            return None
        total = 0
        first = len(nodes) - 1
        for index in range(len(nodes) - 1, -1, -1):
            node = nodes[index]
            if node.content_type != "agent_message":
                continue
            cost = max(1, len(json.dumps(agent_message_to_dict(node.content))) // 4)
            if index < len(nodes) - 1 and total + cost > self._preserve_tail_tokens:
                break
            total += cost
            first = index
        # 活动分支不能从孤立的 Assistant 开始，否则模型会看到没有对应问题的
        # 答案。优先把紧邻的 User 一并保留；ToolResult 配对由 A3 硬规则保证。
        if (
            first < len(nodes)
            and isinstance(nodes[first].content, AssistantMessage)
            and first > 0
            and isinstance(nodes[first - 1].content, UserMessage)
        ):
            first -= 1
        return first if first > 0 else None

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
            return agent_message_to_dict(node.content)
        content = node.content
        return {
            "content_type": "history_compaction",
            "summary": content.summary,
            "first_kept_node_id": content.first_kept_node_id,
        }

    @staticmethod
    def _summary_text(message: AssistantMessage) -> str:
        return "\n".join(
            block.text
            for block in message.content
            if isinstance(block, TextBlock) and block.text
        ).strip()
