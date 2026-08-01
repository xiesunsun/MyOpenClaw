"""`.mcp.json` 的发现、解析与合并。

格式与 Claude Code 兼容：{"mcpServers": {"<name>": {"command", "args", "env"}}}。
任何一层失败都不抛：坏文件整体跳过、坏 server 单独跳过，记录 warning
和脱敏诊断——配置问题不该阻断启动（与 extension 装载失败隔离同语义）。
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
    config_scope: str | None = None


@dataclass(frozen=True)
class McpConfigLoadResult:
    servers: dict[str, McpServerSpec] = field(default_factory=dict)
    diagnostics: tuple[str, ...] = ()


def load_mcp_servers(*, home: Path, project_root: Path) -> dict[str, McpServerSpec]:
    """全局 home/.mcp.json + 项目 project_root/.mcp.json 合并，项目覆盖同名。"""
    return load_mcp_config(home=home, project_root=project_root).servers


def load_mcp_config(*, home: Path, project_root: Path) -> McpConfigLoadResult:
    """加载 server 与脱敏诊断；项目配置覆盖全局同名 server。"""
    merged: dict[str, McpServerSpec] = {}
    diagnostics: list[str] = []
    for path, scope in (
        (home / ".mcp.json", "global"),
        (project_root / ".mcp.json", "project"),
    ):
        result = _load_file(path, scope=scope)
        merged.update(result.servers)
        diagnostics.extend(result.diagnostics)
    return McpConfigLoadResult(servers=merged, diagnostics=tuple(diagnostics))


def _load_file(path: Path, *, scope: str) -> McpConfigLoadResult:
    if not path.is_file():
        return McpConfigLoadResult()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw_servers = data["mcpServers"]
        if not isinstance(raw_servers, dict):
            raise TypeError("mcpServers must be an object")
    except Exception:
        logger.warning("Ignoring invalid .mcp.json at %s", path, exc_info=True)
        return McpConfigLoadResult(diagnostics=(f"Invalid MCP config: {path}",))
    specs: dict[str, McpServerSpec] = {}
    diagnostics: list[str] = []
    for raw_name, raw in raw_servers.items():
        name = str(raw_name)
        if "__" in name:
            logger.warning("Skipping MCP server '%s': name must not contain '__'", name)
            diagnostics.append(f"Invalid MCP server name '{name}': '__' is not allowed")
            continue
        try:
            if not isinstance(raw, dict):
                raise TypeError("server config must be an object")
            missing_env: list[str] = []
            specs[name] = McpServerSpec(
                name=name,
                command=str(raw["command"]),
                args=tuple(str(item) for item in raw.get("args", [])),
                env={
                    key: _expand(str(value), missing=missing_env)
                    for key, value in (raw.get("env") or {}).items()
                },
                config_scope=scope,
            )
            diagnostics.extend(
                f"MCP server '{name}': environment variable {variable} is not set"
                for variable in dict.fromkeys(missing_env)
            )
        except Exception:
            logger.warning(
                "Skipping invalid MCP server '%s' in %s", name, path, exc_info=True
            )
            diagnostics.append(f"Invalid MCP server configuration: {name}")
    return McpConfigLoadResult(servers=specs, diagnostics=tuple(diagnostics))


def _expand(value: str, *, missing: list[str] | None = None) -> str:
    def _replace(match: re.Match[str]) -> str:
        env_value = os.environ.get(match.group(1))
        if env_value is None:
            logger.warning("MCP env: %s is not set; keeping literal", match.group(0))
            if missing is not None:
                missing.append(match.group(1))
            return match.group(0)
        return env_value

    return _ENV_PATTERN.sub(_replace, value)
