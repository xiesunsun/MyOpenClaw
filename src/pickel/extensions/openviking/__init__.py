"""OpenViking extension：会话同步与按 query 的记忆召回。

行为变更（相对迁移前的 boot 路径）：openviking 启用但某 agent 无
remote_agent_id 时，旧 boot._resolve_openviking_remote_agent_id 抛 ValueError；
工厂路径改为返回 None（该 agent 不启用）—— 一个 extension 配不全
不该让 agent 起不来。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pickel.config.paths import sessions_db_path
from pickel.extensions.openviking.bypass_store import OpenVikingBypassStore
from pickel.extensions.openviking.commit_policy import ThresholdCommitPolicy
from pickel.extensions.openviking.config import OpenVikingConfig
from pickel.extensions.openviking.context_client import SyncHTTPOpenVikingContextClient
from pickel.extensions.openviking.recall_adapter import OpenVikingRecall
from pickel.extensions.openviking.session_client import SyncHTTPOpenVikingSessionClient
from pickel.extensions.openviking.session_message_mapper import SessionMessageMapper
from pickel.extensions.openviking.session_recall import OpenVikingSessionRecallProvider
from pickel.extensions.openviking.session_sync import OpenVikingSessionSync


def setup(host) -> None:
    config = host.config(OpenVikingConfig)
    if config is None or not config.enabled:
        return

    host.add_session_sync(lambda scope: _make_session_sync(config, scope))
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


def _make_session_sync(config: OpenVikingConfig, scope) -> Any | None:
    remote_agent_id = _resolve_remote_agent_id(config, scope)
    if remote_agent_id is None:
        return None
    db_path = sessions_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return OpenVikingSessionSync(
        config=config,
        remote_agent_id=remote_agent_id,
        client=SyncHTTPOpenVikingSessionClient(config, remote_agent_id=remote_agent_id),
        message_mapper=SessionMessageMapper(
            tool_output_max_chars=config.tool_output_max_chars
        ),
        commit_policy=ThresholdCommitPolicy(
            commit_after=timedelta(minutes=config.commit_after_minutes),
            commit_after_turns=config.commit_after_turns,
        ),
        # OpenViking 游标旁路表，与 Session 核心解耦；与 sessions.db 同库
        state_store=OpenVikingBypassStore(db_path),
    )


def _make_recall(config: OpenVikingConfig, scope) -> Any | None:
    remote_agent_id = _resolve_remote_agent_id(config, scope)
    if remote_agent_id is None:
        return None
    db_path = sessions_db_path()
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
