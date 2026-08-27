"""Runtime 进程生命周期：配置、Extension、Host 的唯一装配入口。"""

from __future__ import annotations

from pickel.app.runtime_host import RuntimeHost
from pickel.app.runtime_models import RuntimeLaunchRequest
from pickel.config.loader import Config
from pickel.config.paths import runtime_db_path
from pickel.conversations.conversation_service import ConversationNotFoundError
from pickel.persistence.sqlite_runtime_store import SQLiteRuntimeStore


class RuntimeApplication:
    """供 CLI/TUI/协议 Surface 共享的异步进程生命周期。"""

    def __init__(self, request: RuntimeLaunchRequest) -> None:
        self.request = request
        self.cwd = request.cwd.resolve()
        self.host: RuntimeHost | None = None

    @classmethod
    def open(cls, request: RuntimeLaunchRequest) -> "RuntimeApplication":
        return cls(request)

    @property
    def warnings(self) -> tuple[str, ...]:
        if self.host is None:
            return ()
        return tuple(str(error) for error in self.host.load_result.errors)

    @property
    def load_result(self):
        """兼容旧 Surface；装配前没有 Extension LoadResult。"""

        return self.host.load_result if self.host is not None else None

    async def __aenter__(self) -> "RuntimeApplication":
        app_config = Config.load(cwd=self.cwd)
        launch_agent_ids = self._resolve_launch_agent_ids()
        self.host = await RuntimeHost.create(
            app_config,
            launch_agent_ids=launch_agent_ids,
        )
        return self

    def _resolve_launch_agent_ids(self) -> tuple[str, ...] | None:
        session_id = self.request.session_id
        if session_id is None:
            return self.request.agent_ids
        session = SQLiteRuntimeStore(runtime_db_path()).load_session(session_id)
        if session is None:
            raise ConversationNotFoundError(f"ConversationSession 不存在: {session_id}")
        return (session.agent_id,)

    async def __aexit__(self, *_args: object) -> None:
        if self.host is not None:
            await self.host.shutdown()
            self.host = None
