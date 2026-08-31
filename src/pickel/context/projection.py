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
        messages: list[AgentMessage] = []
        if nodes[0].content_type == "history_compaction":
            checkpoint = nodes[0].content
            if not isinstance(checkpoint, HistoryCompaction):
                raise TypeError("history_compaction 节点必须包含 HistoryCompaction")
            messages.append(
                UserMessage(
                    content=(TextBlock(text=f"[compaction]\n{checkpoint.summary}"),)
                )
            )
            messages.extend(checkpoint.retained_messages)
            tail = nodes[1:]
        else:
            tail = nodes

        if any(node.content_type != "agent_message" for node in tail):
            raise ValueError(
                "ConversationProjector 输入合同无效: checkpoint 只能是首节点"
            )
        messages.extend(node.content for node in tail)
        return messages
