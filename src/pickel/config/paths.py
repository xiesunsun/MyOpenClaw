"""pickel 路径：用户家目录、会话库、项目根发现。"""

from __future__ import annotations

import os
from pathlib import Path

_ENV_HOME = "PICKEL_HOME"
_HOME_DIR_NAME = ".pickel"
_PROJECT_MARKER = ".pickel"
_AGENTS_DIR = "agents"
_SESSIONS_DB = "sessions.db"
_RUNTIME_DB = "runtime.db"


def home_dir() -> Path:
    """用户 pickel 家目录；环境变量 PICKEL_HOME 可覆盖（测试用）。"""
    override = os.environ.get(_ENV_HOME)
    if override:
        return Path(override)
    return Path.home() / _HOME_DIR_NAME


def sessions_db_path() -> Path:
    """全局会话库路径：{home_dir}/sessions.db。"""
    return home_dir() / _SESSIONS_DB


def runtime_db_path() -> Path:
    """Agent Runtime v4+ 原子存储路径。"""
    return home_dir() / _RUNTIME_DB


def artifact_blobs_path() -> Path:
    """默认本地 BlobStore 根目录。"""
    return home_dir() / "artifacts" / "blobs"


def discover_project_root(cwd: Path) -> Path | None:
    """从 cwd 向上查找含 `.pickel` 或 `agents`（目录）的目录。"""
    current = Path(cwd).resolve()
    for candidate in (current, *current.parents):
        if (candidate / _PROJECT_MARKER).is_dir():
            return candidate
        if (candidate / _AGENTS_DIR).is_dir():
            return candidate
    return None
