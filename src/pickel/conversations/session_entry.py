"""SessionEntry：append-only 对话树节点。

payload 为已版本化的 JSON-ready dict：
- message：agent_message_to_dict 结果（payload_version=1）
- compaction：含 summary / first_kept_entry_id 等（payload_version=1）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

ENTRY_TYPE_MESSAGE = "message"
ENTRY_TYPE_COMPACTION = "compaction"
ENTRY_TYPE_HOST_CALL_REQUEST = "host_call_request"
ENTRY_TYPE_HOST_CALL_RESPONSE = "host_call_response"

MESSAGE_PAYLOAD_VERSION = 1
COMPACTION_PAYLOAD_VERSION = 1
HOST_CALL_PAYLOAD_VERSION = 1


@dataclass(frozen=True)
class SessionEntry:
    """单条会话 entry；创建后字段不可变，禁止原地改 payload。"""

    entry_id: str
    session_id: str
    parent_id: str | None
    entry_type: str
    payload: dict[str, Any]
    created_at: datetime
