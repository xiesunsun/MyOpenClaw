"""Runtime 进程生命周期：配置、Extension、Host 的唯一装配入口。"""

from __future__ import annotations

from pathlib import Path

from pickel.app.boot import Boot
from pickel.app.runtime import RuntimeHost
from pickel.config.loader import Config
from pickel.extensions_host.loader import (
    LoadResult,
    load_extensions_async,
    teardown_extensions,
)
from pickel.tools.bus import ToolBus
from pickel.tools.catalog import install_builtin_tools


class RuntimeApplication:
    """供 CLI/TUI/协议 Surface 共享的异步进程生命周期。"""

    def __init__(self, *, cwd: Path) -> None:
        self.cwd = cwd.resolve()
        self.host: RuntimeHost | None = None
        self.load_result: LoadResult | None = None

    @classmethod
    def open(cls, *, cwd: Path) -> "RuntimeApplication":
        return cls(cwd=cwd)

    @property
    def warnings(self) -> tuple[str, ...]:
        result = self.load_result
        if result is None:
            return ()
        return tuple(str(error) for error in result.errors)

    async def __aenter__(self) -> "RuntimeApplication":
        app_config = Config.load(cwd=self.cwd)
        tool_bus = ToolBus()
        install_builtin_tools(tool_bus)
        result = await load_extensions_async(
            tool_bus=tool_bus,
            app_config=app_config,
        )
        try:
            boot = Boot.from_config(
                app_config,
                tool_bus=tool_bus,
                extensions=result.registry,
            )
        except BaseException:
            await teardown_extensions(result, tool_bus=tool_bus)
            raise
        boot.extension_result = result
        self.load_result = result
        self.host = RuntimeHost(boot)
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self.host is not None:
            await self.host.shutdown()
            self.host = None
