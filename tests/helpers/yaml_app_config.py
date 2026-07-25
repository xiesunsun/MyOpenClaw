"""测试辅助：用 yaml 文本构造 AppConfig（非运行时路径；运行时只用 Config.load）。"""

from __future__ import annotations

from pathlib import Path

import yaml

from pickel.config.app_config import AppConfig, expand_env_vars


def app_config_from_yaml_file(path: Path) -> AppConfig:
    data = expand_env_vars(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    if not isinstance(data, dict):
        raise TypeError("yaml root must be mapping")
    data["root"] = path.parent
    return AppConfig.model_validate(data)


def write_layered_home(
    home: Path,
    *,
    settings: dict,
    models: dict | None = None,
    auth: dict | None = None,
) -> None:
    import json

    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps(settings), encoding="utf-8"
    )
    if models is not None:
        (home / "models.json").write_text(json.dumps(models), encoding="utf-8")
    if auth is not None:
        (home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
