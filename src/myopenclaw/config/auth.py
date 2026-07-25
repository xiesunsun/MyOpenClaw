"""读取 auth.json（仅全局）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from myopenclaw.config.app_config import expand_env_vars

AUTH_FILENAME = "auth.json"


def auth_path(home: Path) -> Path:
    """全局 auth.json 路径。"""
    return Path(home) / AUTH_FILENAME


def load_auth(path: Path) -> dict[str, Any]:
    """读取 auth.json；文件不存在返回空 dict。展开 ${ENV}。

    期望结构：
    {
      "providers": { "<id>": {"api_key": "...", "api_base": "..."} },
      "openviking": { "base_url", "account_id", "user_id", "user_key", ... }
    }
    """
    file_path = Path(path)
    if not file_path.is_file():
        return {}
    with file_path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"auth.json 须为对象: {file_path}")
    return expand_env_vars(data)
