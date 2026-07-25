"""读取 settings.json（全局 / 项目）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from myopenclaw.config.app_config import expand_env_vars

SETTINGS_FILENAME = "settings.json"


def settings_path(base: Path) -> Path:
    """settings.json 完整路径。base 为 home 或 project/.pickel。"""
    return Path(base) / SETTINGS_FILENAME


def load_settings(path: Path) -> dict[str, Any]:
    """读取 settings.json；文件不存在返回空 dict。展开 ${ENV}。"""
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    with file_path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"settings.json 须为对象: {file_path}")
    return expand_env_vars(data)
