"""MCP 客户端 extension：把 stdio MCP server 的工具接入工具总线。

server 列表读 .mcp.json（~/.pickel/ 全局 + workspace 项目级，项目覆盖同名）；
extension 启停沿用 settings.json 的 extensions.mcp.enabled（默认开）。
单 server 失败隔离：记 warning 跳过，不阻断启动。
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from pickel.config.paths import home_dir
from pickel.extensions.mcp.config import load_mcp_servers
from pickel.extensions.mcp.runtime import McpServerRuntime

logger = logging.getLogger(__name__)

_runtimes: list[McpServerRuntime] = []


class McpExtensionConfig(BaseModel):
    enabled: bool = True


async def setup(host) -> None:
    config = host.config(McpExtensionConfig)
    if config is not None and not config.enabled:
        return
    project_root = getattr(host.app_config, "root", None)
    if project_root is None:
        logger.warning("MCP extension: app_config.root unavailable; skipping")
        return
    specs = load_mcp_servers(home=home_dir(), project_root=project_root)
    for spec in specs.values():
        runtime = McpServerRuntime(spec=spec, host=host)
        try:
            await runtime.start()
        except Exception:
            logger.warning(
                "MCP server '%s' failed to start; skipping", spec.name, exc_info=True
            )
            continue
        _runtimes.append(runtime)
        logger.info("MCP server '%s' connected", spec.name)


async def teardown() -> None:
    for runtime in _runtimes:
        try:
            await runtime.close()
        except Exception:
            logger.exception("MCP server '%s' close failed", runtime.spec.name)
    _runtimes.clear()
