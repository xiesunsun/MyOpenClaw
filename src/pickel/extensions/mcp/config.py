"""`.mcp.json` 的发现、解析与合并。

格式与 Claude Code 兼容：{"mcpServers": {"<name>": {"command", "args", "env"}}}。
任何一层失败都不抛：坏文件整体跳过、坏 server 单独跳过，只记 warning——
配置问题不该阻断启动（与 extension 装载失败隔离同语义）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re

logger = logging.getLogger(__name__)

_ENV_PATTERN = re.compile(r"\$\{(\w+)\}")


@dataclass(frozen=True)
class McpServerSpec:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


def load_mcp_servers(*, home: Path, project_root: Path) -> dict[str, McpServerSpec]:
    """全局 home/.mcp.json + 项目 project_root/.mcp.json 合并，项目覆盖同名。"""
    merged: dict[str, McpServerSpec] = {}
    for path in (home / ".mcp.json", project_root / ".mcp.json"):
        merged.update(_load_file(path))
    return merged


def _load_file(path: Path) -> dict[str, McpServerSpec]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw_servers = data["mcpServers"]
    except Exception:
        logger.warning("Ignoring invalid .mcp.json at %s", path, exc_info=True)
        return {}
    specs: dict[str, McpServerSpec] = {}
    for name, raw in raw_servers.items():
        if "__" in name:
            logger.warning(
                "Skipping MCP server '%s': name must not contain '__'", name
            )
            continue
        try:
            specs[name] = McpServerSpec(
                name=name,
                command=str(raw["command"]),
                args=tuple(str(item) for item in raw.get("args", [])),
                env={
                    key: _expand(str(value))
                    for key, value in (raw.get("env") or {}).items()
                },
            )
        except Exception:
            logger.warning(
                "Skipping invalid MCP server '%s' in %s", name, path, exc_info=True
            )
    return specs


def _expand(value: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        env_value = os.environ.get(match.group(1))
        if env_value is None:
            logger.warning("MCP env: %s is not set; keeping literal", match.group(0))
            return match.group(0)
        return env_value

    return _ENV_PATTERN.sub(_replace, value)
