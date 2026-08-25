"""Conversation Tree 固定 active leaf 到模型消息的投影。"""

from __future__ import annotations

from collections.abc import Sequence

from pickel.conversations.agent_message import AgentMessage, UserMessage
from pickel.conversations.content_blocks import TextBlock
from pickel.conversations.conversation_node import ConversationNode, HistoryCompaction


class ConversationProjector:
    """只消费 Store 已按 parent 链返回的 ConversationNode 路径。"""

    def project_conversation_messages(
        self, nodes: Sequence[ConversationNode]
    ) -> list[AgentMessage]:
        if not nodes:
            return []

        # 从 leaf 向前查找最后一个合法压缩。后续节点可能属于新分支，
        # 但输入路径本身已经由 Store 固定为单条 active leaf 链。
        for index in range(len(nodes) - 1, -1, -1):
            node = nodes[index]
            if node.content_type != "history_compaction":
                continue
            result = self._project_with_compaction(nodes, index, node.content)
            if result is not None:
                return result
        return [node.content for node in nodes if node.content_type == "agent_message"]

    def _project_with_compaction(
        self,
        nodes: Sequence[ConversationNode],
        compaction_index: int,
        compaction: HistoryCompaction,
    ) -> list[AgentMessage] | None:
        first_kept_node_id = compaction.first_kept_node_id
        if first_kept_node_id is None:
            return None
        first_kept_index = next(
            (
                index
                for index, node in enumerate(nodes[:compaction_index])
                if node.node_id == first_kept_node_id
            ),
            None,
        )
        if first_kept_index is None:
            return None

        messages: list[AgentMessage] = [
            UserMessage(
                content=(TextBlock(text=f"[compaction]\n{compaction.summary}"),)
            )
        ]
        messages.extend(
            node.content
            for node in nodes[first_kept_index:]
            if node.content_type == "agent_message"
        )
        return messages
