"""读写 settings.json（全局 / 项目）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Literal

from myopenclaw.config.app_config import expand_env_vars
from myopenclaw.config.paths import discover_project_root, home_dir
from myopenclaw.shared.model_config import ModelSelection

SETTINGS_FILENAME = "settings.json"


def settings_path(base: Path) -> Path:
    """settings.json 完整路径。base 为 home 或 project/.pickel。"""
    return Path(base) / SETTINGS_FILENAME


def _load_settings_raw(path: Path) -> dict[str, Any]:
    """读取 settings.json 原文；文件不存在返回空 dict。不展开 ${ENV}。"""
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    with file_path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"settings.json 须为对象: {file_path}")
    return data


def load_settings(path: Path) -> dict[str, Any]:
    """读取 settings.json；文件不存在返回空 dict。展开 ${ENV}。"""
    return expand_env_vars(_load_settings_raw(path))


@contextmanager
def _file_lock(lock_path: Path) -> Iterator[None]:
    """POSIX 下对 lock 文件加排他锁；其他平台为空操作。"""
    if sys.platform == "win32":
        yield
        return
    import fcntl

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def save_settings(path: Path, data: dict[str, Any]) -> None:
    """原子写入 settings.json（tempfile + replace）；POSIX 可选 fcntl 锁。"""
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = file_path.with_suffix(file_path.suffix + ".lock")
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"

    with _file_lock(lock_path):
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{file_path.name}.",
            suffix=".tmp",
            dir=file_path.parent,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.replace(file_path)
        except Exception:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            raise


def update_settings(path: Path, patch: dict[str, Any]) -> dict[str, Any]:
    """加载（原文）、deep_merge patch、写回；返回合并后的 dict。"""
    # 延迟导入，避免与 loader 循环依赖
    from myopenclaw.config.loader import deep_merge

    file_path = Path(path)
    lock_path = file_path.with_suffix(file_path.suffix + ".lock")
    with _file_lock(lock_path):
        current = _load_settings_raw(file_path)
        merged = deep_merge(current, patch)
        # 锁内直接写，避免 save_settings 再抢同一锁
        file_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{file_path.name}.",
            suffix=".tmp",
            dir=file_path.parent,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temp_path.replace(file_path)
        except Exception:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)
            raise
        return merged


def set_default_llm(
    selection: ModelSelection,
    *,
    scope: Literal["global", "project"] = "global",
    home: Path | None = None,
    project_root: Path | None = None,
) -> Path:
    """将 default_llm 写入全局或项目 settings.json；返回写入路径。"""
    if scope == "global":
        base = Path(home) if home is not None else home_dir()
        path = settings_path(base)
    else:
        root = (
            Path(project_root)
            if project_root is not None
            else discover_project_root(Path.cwd())
        )
        if root is None:
            raise ValueError("未找到项目根（含 .pickel 或 agents 目录），无法写项目 settings")
        path = settings_path(root / ".pickel")

    patch = {
        "default_llm": {
            "provider": selection.provider,
            "model": selection.model,
        }
    }
    update_settings(path, patch)
    return path
