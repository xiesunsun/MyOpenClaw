"""MCP extension 的进程级状态所有者。"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from pickel.extensions.mcp.config import McpServerSpec
from pickel.extensions.mcp.runtime import McpServerRuntime
from pickel.extensions_host.mcp_status import McpStatusSnapshot

logger = logging.getLogger(__name__)


class McpExtensionState:
    """持有全部 server runtime，包括启动失败但需要被观察的 server。"""

    def __init__(self, *, diagnostics: Iterable[str] = ()) -> None:
        self._runtimes: dict[str, McpServerRuntime] = {}
        self._diagnostics = tuple(diagnostics)

    async def start(self, specs: Iterable[McpServerSpec], *, host: Any) -> None:
        for spec in specs:
            runtime = McpServerRuntime(spec=spec, host=host)
            self._runtimes[spec.name] = runtime
            try:
                await runtime.start()
            except Exception:
                logger.warning(
                    "MCP server '%s' failed to start; skipping",
                    spec.name,
                    exc_info=True,
                )
                continue
            logger.info("MCP server '%s' connected", spec.name)

    async def close(self) -> None:
        for runtime in self._runtimes.values():
            try:
                await runtime.close()
            except Exception:
                logger.exception("MCP server '%s' close failed", runtime.spec.name)
        self._runtimes.clear()

    def snapshot(self) -> McpStatusSnapshot:
        return McpStatusSnapshot(
            servers=tuple(
                self._runtimes[name].snapshot() for name in sorted(self._runtimes)
            ),
            diagnostics=self._diagnostics,
        )
