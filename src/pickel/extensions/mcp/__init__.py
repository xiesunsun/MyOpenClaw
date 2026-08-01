"""MCP 客户端 extension：把 stdio MCP server 的工具接入工具总线。

server 列表读 .mcp.json（~/.pickel/ 全局 + workspace 项目级，项目覆盖同名）；
extension 启停沿用 settings.json 的 extensions.mcp.enabled（默认开）。
单 server 失败隔离：记 warning 跳过，不阻断启动。
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from pickel.config.paths import home_dir
from pickel.extensions.mcp.config import load_mcp_config
from pickel.extensions.mcp.state import McpExtensionState

logger = logging.getLogger(__name__)

_state: McpExtensionState | None = None


class McpExtensionConfig(BaseModel):
    enabled: bool = True


async def setup(host) -> None:
    global _state
    config = host.config(McpExtensionConfig)
    if config is not None and not config.enabled:
        return
    project_root = getattr(host.app_config, "root", None)
    if project_root is None:
        logger.warning("MCP extension: app_config.root unavailable; skipping")
        return
    loaded = load_mcp_config(home=home_dir(), project_root=project_root)
    state = McpExtensionState(diagnostics=loaded.diagnostics)
    host.register_mcp_status_source(state)
    _state = state
    await state.start(loaded.servers.values(), host=host)


async def teardown() -> None:
    global _state
    if _state is not None:
        await _state.close()
        _state = None
