"""HistoryCompaction 的共享应用服务。"""

from __future__ import annotations

from pickel.context.history_compaction import (
    HistoryCompactionError,
    HistoryCompactionGenerator,
    SummarizerSender,
)
from pickel.context.model_context import ModelContext
from pickel.conversations.agent_message import (
    AgentMessage,
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.conversation_node import ConversationNode, HistoryCompaction
from pickel.conversations.conversation_service import ConversationService


class HistoryCompactionService:
    """校验 leaf、准备逻辑历史并以 leaf CAS 提交一个 checkpoint。"""

    def __init__(
        self,
        conversation_service: ConversationService,
        generator: HistoryCompactionGenerator,
    ) -> None:
        self._conversations = conversation_service
        self._generator = generator

    async def compact(
        self,
        *,
        session_id: str,
        expected_leaf_node_id: str | None,
        model_context: ModelContext | None,
        send_summarizer: SummarizerSender,
        max_summary_tokens: int,
        preserve_tail_tokens: int,
        worker_input_limit: int,
    ) -> ConversationNode:
        session = self._conversations.load_conversation_session(session_id)
        if session.active_node_id != expected_leaf_node_id:
            raise HistoryCompactionError(
                "history_compaction_leaf_conflict",
                "压缩 leaf 已变化，拒绝把旧摘要挂到新历史上",
            )
        nodes = self._conversations.list_context_nodes(
            session_id=session_id, leaf_node_id=expected_leaf_node_id
        )
        previous_summary: str | None = None
        previous_read_files: tuple[str, ...] = ()
        previous_modified_files: tuple[str, ...] = ()
        exact_messages: list[AgentMessage] = []
        if nodes and nodes[0].content_type == "history_compaction":
            checkpoint = nodes[0].content
            if not isinstance(checkpoint, HistoryCompaction):
                raise HistoryCompactionError(
                    "history_compaction_invalid_result", "checkpoint 内容类型无效"
                )
            previous_summary = checkpoint.summary
            previous_read_files = checkpoint.read_files
            previous_modified_files = checkpoint.modified_files
            exact_messages.extend(checkpoint.retained_messages)
            nodes = nodes[1:]
        elif any(node.content_type == "history_compaction" for node in nodes):
            raise HistoryCompactionError(
                "history_compaction_invalid_result",
                "Context 节点中的 checkpoint 位置无效",
            )
        for node in nodes:
            if node.content_type != "agent_message" or not isinstance(
                node.content, (UserMessage, AssistantMessage, ToolResultMessage)
            ):
                raise HistoryCompactionError(
                    "history_compaction_invalid_result", "Context 节点消息类型无效"
                )
            exact_messages.append(node.content)
        content = await self._generator.generate(
            previous_summary=previous_summary,
            exact_messages=tuple(exact_messages),
            previous_read_files=previous_read_files,
            previous_modified_files=previous_modified_files,
            model_context=model_context,
            worker_input_limit=worker_input_limit,
            send_summarizer=send_summarizer,
            max_summary_tokens=max_summary_tokens,
            preserve_tail_tokens=preserve_tail_tokens,
        )
        if not isinstance(content, HistoryCompaction):
            raise HistoryCompactionError(
                "history_compaction_invalid_result",
                "HistoryCompactionGenerator 必须返回 HistoryCompaction",
            )
        return self._conversations.append_history_compaction_at_leaf(
            session_id=session_id,
            expected_leaf_node_id=expected_leaf_node_id,
            content=content,
        )
