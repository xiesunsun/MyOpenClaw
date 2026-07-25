"""Config：分层读取 settings / models / auth，合并为 AppConfig。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from pickel.config.agents import scan_agents
from pickel.config.app_config import AppConfig, expand_env_vars
from pickel.config.auth import auth_path, load_auth
from pickel.config.models_catalog import load_models, models_path
from pickel.config.paths import discover_project_root, home_dir
from pickel.config.settings import load_settings, settings_path

_PROJECT_DIR = ".pickel"
_LEGACY_CONFIG_YAML = "config.yaml"

# 内置默认（最低优先级）
_BUILTIN_DEFAULTS: dict[str, Any] = {
    "default_file_access_mode": "workspace",
    "react_max_steps": 8,
    "context_cli_turn_window": 5,
    "providers": {},
    "agents": {},
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """深度合并：dict 递归，数组与标量整键替换。不修改入参。"""
    result: dict[str, Any] = dict(base)
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_legacy_yaml(project_root: Path) -> dict[str, Any]:
    """过渡：读取项目根 config.yaml 全文（未 migrate 时仍可启动）。"""
    legacy = project_root / _LEGACY_CONFIG_YAML
    if not legacy.is_file():
        return {}
    with legacy.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        return {}
    return expand_env_vars(data)


class Config:
    """配置加载入口：合并磁盘分层 → AppConfig。"""

    @classmethod
    def load(
        cls,
        cwd: Path | None = None,
        home: Path | None = None,
    ) -> AppConfig:
        """加载并合并配置。

        合并顺序（低 → 高）：
        内置默认 < 项目 config.yaml（过渡）< 全局 settings/models/auth
        < 项目 .pickel settings/models
        agents：上述中的 agents < 目录 agents/（同 id 目录优先）
        """
        resolved_cwd = Path(cwd) if cwd is not None else Path.cwd()
        resolved_home = Path(home) if home is not None else home_dir()
        project_root = discover_project_root(resolved_cwd)
        root = project_root if project_root is not None else resolved_cwd.resolve()

        global_settings = load_settings(settings_path(resolved_home))
        global_models = load_models(models_path(resolved_home))
        global_auth = load_auth(auth_path(resolved_home))

        project_settings: dict[str, Any] = {}
        project_models: dict[str, Any] = {}
        legacy: dict[str, Any] = {}
        if project_root is not None:
            project_dir = project_root / _PROJECT_DIR
            project_settings = load_settings(settings_path(project_dir))
            project_models = load_models(models_path(project_dir))
            legacy = _load_legacy_yaml(project_root)

        # settings 字段：默认 < legacy yaml < 全局 < 项目 .pickel
        merged = deep_merge(_BUILTIN_DEFAULTS, legacy)
        merged = deep_merge(merged, global_settings)
        merged = deep_merge(merged, project_settings)

        # providers：legacy 内嵌 < models.json 全局 < 项目 < settings 里偶发 providers
        providers: dict[str, Any] = {}
        if isinstance(legacy.get("providers"), dict):
            providers = deep_merge(providers, legacy["providers"])
        providers = deep_merge(providers, global_models.get("providers") or {})
        providers = deep_merge(providers, project_models.get("providers") or {})
        if isinstance(merged.get("providers"), dict):
            providers = deep_merge(providers, merged["providers"])
        merged["providers"] = providers

        # openviking：settings/legacy 策略 + auth 密钥
        openviking = merged.get("openviking")
        auth_ov = global_auth.get("openviking")
        if isinstance(openviking, dict) or isinstance(auth_ov, dict):
            ov_merged: dict[str, Any] = {}
            if isinstance(openviking, dict):
                ov_merged = deep_merge(ov_merged, openviking)
            if isinstance(auth_ov, dict):
                ov_merged = deep_merge(ov_merged, auth_ov)
            merged["openviking"] = ov_merged if ov_merged else None
        else:
            merged.pop("openviking", None)

        # agents：merged（含 legacy）< 目录 agents/（同 id 目录优先）
        agents: dict[str, Any] = {}
        if isinstance(merged.get("agents"), dict):
            agents = deep_merge(agents, merged["agents"])
        if project_root is not None:
            agents = deep_merge(agents, scan_agents(project_root))
        merged["agents"] = agents

        auth_providers = global_auth.get("providers") or {}
        if not isinstance(auth_providers, dict):
            auth_providers = {}

        merged["root"] = root
        merged["auth_providers"] = auth_providers

        return AppConfig.model_validate(merged)
