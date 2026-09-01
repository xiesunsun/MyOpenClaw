"""读取 models.json（全局 / 项目）。命名避开 model_config。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pickel.config.app_config import expand_env_vars

MODELS_FILENAME = "models.json"


def models_path(base: Path) -> Path:
    """models.json 完整路径。base 为 home 或 project/.pickel。"""
    return Path(base) / MODELS_FILENAME


def load_models(path: Path) -> dict[str, Any]:
    """读取 models.json；文件不存在返回空 dict。展开 ${ENV}。

    期望结构：{"providers": { "<id>": {"models": {...}} }}
    """
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    with file_path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"models.json 须为对象: {file_path}")
    return expand_env_vars(data)
