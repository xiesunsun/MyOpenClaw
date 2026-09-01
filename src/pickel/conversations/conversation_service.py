"""ConversationSession 的直接实体写入与查询服务。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from uuid import uuid4

from pickel.conversations.agent_message import (
    AgentMessage,
    AssistantMessage,
    ToolResultMessage,
    UserMessage,
)
from pickel.conversations.conversation_node import ConversationNode, HistoryCompaction
from pickel.conversations.conversation_session import ConversationSession
from pickel.conversations.conversation_store import ConversationStore
from pickel.conversations.session_preview import (
    SessionPreview,
    build_conversation_preview,
)
from pickel.workspaces.workspace import Workspace


class ConversationNotFoundError(LookupError):
    pass


class ConversationService:
    """通过 v10 Store 的实体/CAS 方法操作 Conversation Tree。"""

    def __init__(
        self,
        store: ConversationStore,
        *,
        session_id_factory: Callable[[], str] | None = None,
        node_id_factory: Callable[[], str] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._session_id_factory = session_id_factory or (lambda: str(uuid4()))
        self._node_id_factory = node_id_factory or (lambda: str(uuid4()))
        self._now = now or (lambda: datetime.now(timezone.utc))

    def create_conversation_session(
        self, *, agent_id: str, cwd: str | None = None
    ) -> ConversationSession:
        """原子创建 Workspace 与 Session，不引入 UnitOfWork。"""
        created_at = self._now()
        root = self._normalize_cwd(cwd)
        workspace = Workspace(
            workspace_id=self._workspace_id(root),
            root_path=root,
            created_at=created_at,
        )
        session_id = self._session_id_factory()
        session = ConversationSession(
            session_id=session_id,
            agent_id=agent_id,
            workspace_id=workspace.workspace_id,
            cwd=root,
            active_node_id=None,
            active_operation_id=None,
            title=None,
            title_source=None,
            created_at=created_at,
            updated_at=created_at,
            archived_at=None,
        )
        # Store 的组合方法负责按 root resolve 或创建 Workspace，并在同一事务
        # 插入 Session。服务不先后调用两个 Store。
        self._store.create_session(workspace=workspace, session=session)
        return self.load_conversation_session(session_id)

    def load_conversation_session(self, session_id: str) -> ConversationSession:
        session = self._store.load_session(session_id)
        if session is None:
            raise ConversationNotFoundError(f"ConversationSession 不存在: {session_id}")
        return session

    def commit_generated_title(self, *, session_id: str, title: str) -> bool:
        """以 Session 标题为空为条件提交自动标题。"""
        return self._store.commit_generated_title(
            session_id=session_id, title=title, updated_at=self._now()
        )

    def set_user_title(self, *, session_id: str, title: str) -> bool:
        """保存用户标题；之后自动标题 CAS 不再生效。"""
        return self._store.set_user_title(
            session_id=session_id, title=title, updated_at=self._now()
        )

    def list_active_branch_nodes(self, *, session_id: str) -> list[ConversationNode]:
        session = self.load_conversation_session(session_id)
        return list(
            self._store.list_branch_nodes(
                session_id=session_id, leaf_node_id=session.active_node_id
            )
        )

    def list_branch_nodes(
        self, *, session_id: str, leaf_node_id: str | None
    ) -> list[ConversationNode]:
        """按显式 leaf 投影单条分支；完整性错误交给 Store 向上报告。"""
        self.load_conversation_session(session_id)
        return list(
            self._store.list_branch_nodes(
                session_id=session_id, leaf_node_id=leaf_node_id
            )
        )

    def list_context_nodes(
        self, *, session_id: str, leaf_node_id: str | None
    ) -> list[ConversationNode]:
        """读取供模型使用的边界路径；Store 在最近 checkpoint 处停止。"""
        self.load_conversation_session(session_id)
        return list(
            self._store.list_context_nodes(
                session_id=session_id, leaf_node_id=leaf_node_id
            )
        )

    def list_conversation_previews(
        self,
        *,
        limit: int = 20,
        cwd: str | None = None,
        all_sessions: bool = False,
    ) -> list[SessionPreview]:
        normalized_cwd = None if all_sessions else str(self._normalize_cwd(cwd))
        sessions = self._store.list_sessions(limit=limit, cwd=normalized_cwd)
        return [self.build_conversation_preview(session) for session in sessions]

    def build_conversation_preview(
        self, session: ConversationSession
    ) -> SessionPreview:
        return build_conversation_preview(
            session=session,
            nodes=list(
                self._store.list_branch_nodes(
                    session_id=session.session_id, leaf_node_id=session.active_node_id
                )
            ),
        )

    def archive_conversation_session(self, *, session_id: str) -> None:
        self.load_conversation_session(session_id)
        self._store.archive_session(session_id=session_id, archived_at=self._now())

    def delete_conversation_session(self, *, session_id: str) -> None:
        self.load_conversation_session(session_id)
        self._store.delete_session(session_id=session_id)

    def append_user_message(
        self, *, session_id: str, message: UserMessage
    ) -> ConversationNode:
        return self._append_content(
            session_id=session_id, content_type="agent_message", content=message
        )

    def append_assistant_message(
        self, *, session_id: str, message: AssistantMessage
    ) -> ConversationNode:
        return self._append_content(
            session_id=session_id, content_type="agent_message", content=message
        )

    def append_tool_result_message(
        self, *, session_id: str, message: ToolResultMessage
    ) -> ConversationNode:
        return self._append_content(
            session_id=session_id, content_type="agent_message", content=message
        )

    def append_history_compaction(
        self, *, session_id: str, content: HistoryCompaction
    ) -> ConversationNode:
        return self._append_content(
            session_id=session_id, content_type="history_compaction", content=content
        )

    def append_history_compaction_at_leaf(
        self,
        *,
        session_id: str,
        expected_leaf_node_id: str | None,
        content: HistoryCompaction,
    ) -> ConversationNode:
        """在指定 leaf 后追加 checkpoint；允许该 Session 有 active Operation。"""
        self.load_conversation_session(session_id)
        node = ConversationNode(
            node_id=self._node_id_factory(),
            session_id=session_id,
            parent_node_id=expected_leaf_node_id,
            content_type="history_compaction",
            content=content,
            created_at=self._now(),
        )
        if not self._store.append_history_compaction(
            node=node, expected_node_id=expected_leaf_node_id
        ):
            raise RuntimeError("ConversationSession history_compaction leaf CAS 冲突")
        return node

    def move_active_branch_to(
        self, *, session_id: str, node_id: str
    ) -> ConversationSession:
        session = self.load_conversation_session(session_id)
        if not self._store.move_active_node(
            session_id=session_id,
            expected_node_id=session.active_node_id,
            new_node_id=node_id,
            updated_at=self._now(),
        ):
            raise RuntimeError("ConversationSession active_node_id CAS 冲突")
        return self.load_conversation_session(session_id)

    def _append_content(
        self,
        *,
        session_id: str,
        content_type: str,
        content: AgentMessage | HistoryCompaction,
    ) -> ConversationNode:
        session = self.load_conversation_session(session_id)
        node = ConversationNode(
            node_id=self._node_id_factory(),
            session_id=session_id,
            parent_node_id=session.active_node_id,
            content_type=content_type,  # type: ignore[arg-type]
            content=content,
            created_at=self._now(),
        )
        if not self._store.append_node(
            node=node,
            expected_node_id=session.active_node_id,
        ):
            raise RuntimeError("ConversationSession active_node_id CAS 冲突")
        return node

    @staticmethod
    def _normalize_cwd(cwd: str | None) -> Path:
        return Path(cwd or Path.cwd()).expanduser().resolve(strict=False)

    @staticmethod
    def _workspace_id(root: Path) -> str:
        digest = hashlib.sha256(str(root).encode("utf-8")).hexdigest()
        return f"workspace_{digest}"
