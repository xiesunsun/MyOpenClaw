"""Runtime 进程生命周期：配置、Extension、Host 的唯一装配入口。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pickel.app.runtime_host import RuntimeHost
    from pickel.app.runtime_models import RuntimeLaunchRequest


# 测试和嵌入方可替换此注入点；真实 Runtime 仅在进入执行生命周期时加载。
RuntimeHost: Any = None
SQLiteRuntimeStore: Any = None
ConversationNotFoundError: Any = None
Config: Any = None
runtime_db_path: Any = None


def _load_runtime_dependencies() -> None:
    """推迟 RuntimeHost/Config/SQLite 导入，降低仅构造 Application 的成本。"""
    global RuntimeHost, Config
    if RuntimeHost is None:
        from pickel.app.runtime_host import RuntimeHost as host_type

        RuntimeHost = host_type
    if Config is None:
        from pickel.config.loader import Config as config_type

        Config = config_type


def _load_store_dependencies() -> None:
    global SQLiteRuntimeStore, ConversationNotFoundError, runtime_db_path
    if SQLiteRuntimeStore is None:
        from pickel.persistence.sqlite_runtime_store import (
            SQLiteRuntimeStore as store_type,
        )

        SQLiteRuntimeStore = store_type
    if ConversationNotFoundError is None:
        from pickel.conversations.conversation_service import (
            ConversationNotFoundError as error_type,
        )

        ConversationNotFoundError = error_type
    if runtime_db_path is None:
        from pickel.config.paths import runtime_db_path as path_function

        runtime_db_path = path_function


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
        _load_runtime_dependencies()
        app_config = Config.load(cwd=self.cwd)
        launch_agent_ids = self._resolve_launch_agent_ids()
        self.host = await RuntimeHost.create(
            app_config,
            launch_agent_ids=launch_agent_ids,
        )
        return self

    def _resolve_launch_agent_ids(self) -> tuple[str, ...] | None:
        _load_store_dependencies()
        session_id = self.request.session_id
        if session_id is None:
            return self.request.agent_ids
        store = SQLiteRuntimeStore(runtime_db_path())
        try:
            session = store.load_session(session_id)
        finally:
            store.close()
        if session is None:
            raise ConversationNotFoundError(f"ConversationSession 不存在: {session_id}")
        return (session.agent_id,)

    async def __aexit__(self, *_args: object) -> None:
        if self.host is not None:
            await self.host.shutdown()
            self.host = None
