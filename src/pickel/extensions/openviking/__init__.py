"""OpenViking extension：按 query 提供可选记忆召回。"""

from __future__ import annotations

from typing import Any

from pickel.config.paths import runtime_db_path
from pickel.extensions.openviking.bypass_store import OpenVikingBypassStore
from pickel.extensions.openviking.config import OpenVikingConfig
from pickel.extensions.openviking.context_client import SyncHTTPOpenVikingContextClient
from pickel.extensions.openviking.recall_adapter import OpenVikingRecall
from pickel.extensions.openviking.session_recall import OpenVikingSessionRecallProvider


def setup(host) -> None:
    config = host.config(OpenVikingConfig)
    if config is None or not config.enabled:
        return

    if config.session_recall.enabled:
        host.add_recall_source(lambda scope: _make_recall(config, scope))


def _resolve_remote_agent_id(config: OpenVikingConfig, scope) -> str | None:
    """优先取 config.agents.<id>.remote_agent_id，回落到 agent 自己的配置字段。

    与迁移前 boot._resolve_openviking_remote_agent_id 的优先级保持一致。
    """
    remote_agent_config = config.agents.get(scope.agent_id)
    if remote_agent_config is not None:
        if not remote_agent_config.enabled:
            return None
        return remote_agent_config.remote_agent_id
    return scope.app_config.get_agent_config(scope.agent_id).remote_agent_id


def _make_recall(config: OpenVikingConfig, scope) -> Any | None:
    remote_agent_id = _resolve_remote_agent_id(config, scope)
    if remote_agent_id is None:
        return None
    db_path = runtime_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    provider = OpenVikingSessionRecallProvider(
        config=config,
        client=SyncHTTPOpenVikingContextClient(config, remote_agent_id=remote_agent_id),
        # 与 session_sync 共用同一旁路库，读取 remote_session_id
        state_store=OpenVikingBypassStore(db_path),
    )
    return OpenVikingRecall(
        provider=provider,
        max_chars=config.session_recall.max_chars,
    )
