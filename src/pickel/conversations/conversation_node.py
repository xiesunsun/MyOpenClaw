"""会话树节点与解析后的只读 Entry。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pickel.persistence.immutable_object import ImmutableObject


@dataclass(frozen=True)
class ConversationNode:
    node_id: str
    session_id: str
    parent_node_id: str | None
    object_id: str
    created_commit_sequence: int
    created_at: datetime


@dataclass(frozen=True)
class ConversationEntry:
    """ConversationNode 与其 ImmutableObject 的读取投影。"""

    node: ConversationNode
    object: ImmutableObject
