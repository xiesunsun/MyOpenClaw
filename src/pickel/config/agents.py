"""Agents：扫描项目 agents/<id>/ 目录。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from pickel.config.app_config import expand_env_vars

_AGENTS_DIR = "agents"
_AGENT_YAML = "agent.yaml"
_AGENT_MD_NAMES = ("AGENT.md", "agent.md")

# agent.yaml 可声明字段（behavior 默认取目录，可由 behavior_path 覆盖）
_YAML_FIELDS = frozenset(
    {
        "workspace_path",
        "tools",
        "extensions",
        "file_access_mode",
        "models",
        "remote_agent_id",
        "skills_path",
        "behavior_path",
        "delegation",
    }
)


def scan_agents(project_root: Path) -> dict[str, Any]:
    """扫描 ``{project_root}/agents/*/``，返回 agent_id → 配置 dict。

    含 ``AGENT.md`` / ``agent.md`` 或 ``agent.yaml`` 的子目录即注册。
    """
    return scan_agents_dir(
        Path(project_root) / _AGENTS_DIR,
        project_root=Path(project_root),
    )


def scan_agents_dir(agents_root: Path, *, project_root: Path) -> dict[str, Any]:
    """扫描指定 Agent 根目录；内置与项目 Agent 共用同一份解析合同。"""
    agents_root = Path(agents_root)
    if not agents_root.is_dir():
        return {}

    result: dict[str, Any] = {}
    for child in sorted(agents_root.iterdir()):
        if not child.is_dir():
            continue
        if not _is_agent_dir(child):
            continue
        result[child.name] = load_agent_dir(child, Path(project_root))
    return result


def load_agent_dir(agent_dir: Path, project_root: Path) -> dict[str, Any]:
    """从单个 agent 目录加载配置 dict（路径相对 project_root，供 AppConfig 解析）。"""
    agent_dir = Path(agent_dir)
    project_root = Path(project_root)
    data: dict[str, Any] = {}

    yaml_path = agent_dir / _AGENT_YAML
    if yaml_path.is_file():
        with yaml_path.open(encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"Invalid agent.yaml at {yaml_path}: expected mapping")
        data = expand_env_vars(raw)
        if "llm" in data:
            raise ValueError(f"{yaml_path} 不再支持 llm；请改为 models.primary")

    config: dict[str, Any] = {
        key: value for key, value in data.items() if key in _YAML_FIELDS
    }

    # 行为默认：agents/<id>/（BehaviorLoader 在目录下找 AGENT.md）
    if "behavior_path" not in config:
        config["behavior_path"] = _relative_to_root(agent_dir, project_root)

    if "workspace_path" not in config:
        config["workspace_path"] = "."

    if "tools" not in config:
        config["tools"] = []

    return config


def _is_agent_dir(agent_dir: Path) -> bool:
    if (agent_dir / _AGENT_YAML).is_file():
        return True
    for name in _AGENT_MD_NAMES:
        if (agent_dir / name).is_file():
            return True
    return False


def _relative_to_root(path: Path, project_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(project_root.resolve()))
    except ValueError:
        return str(path)
