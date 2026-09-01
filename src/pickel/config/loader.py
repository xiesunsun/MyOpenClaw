"""Config：分层读取 settings / models / auth / agents，合并为 AppConfig。

唯一运行时加载路径。旧 config.yaml 仅能通过 `pickel config migrate` 导入，运行时不读。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from pickel.config.agents import scan_agents, scan_agents_dir
from pickel.config.app_config import AppConfig
from pickel.config.auth import auth_path, load_auth
from pickel.config.models_catalog import load_models, models_path
from pickel.config.paths import discover_project_root, home_dir
from pickel.config.settings import load_settings, settings_path

_PROJECT_DIR = ".pickel"
_BUILTIN_AGENTS_DIR = Path(__file__).resolve().parent.parent / "agents" / "builtin"

# 内置默认（最低优先级；不含 default_agent / default_llm，须由 settings 提供）
_BUILTIN_DEFAULTS: dict[str, Any] = {
    "default_file_access_mode": "workspace",
    "react_max_steps": 8,
    "model_request_max_attempts": 3,
    "model_request_retry_delays_ms": [20000, 60000, 120000],
    "max_parallel_model_requests": 2,
    "delegation_result_max_chars": 8000,
    "observability": {
        "trace": {
            "mode": "standard",
            "queue_capacity": 8192,
            "batch_size": 128,
            "flush_interval_ms": 250,
            "max_file_size_mb": 64,
            "max_age_days": 14,
            "max_total_size_mb": 1024,
        }
    },
    "providers": {},
    "agents": {},
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """深度合并：dict 递归，数组与标量整键替换。不修改入参。"""
    result: dict[str, Any] = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class Config:
    """配置加载入口：合并磁盘分层 → AppConfig。"""

    @classmethod
    def load(
        cls,
        cwd: Path | None = None,
        home: Path | None = None,
    ) -> AppConfig:
        """加载并合并配置。

        顺序（低 → 高）：
        内置默认 < 全局 ~/.pickel settings/models/auth
        < 项目 .pickel settings/models < 项目 agents/ 目录
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
        if project_root is not None:
            project_dir = project_root / _PROJECT_DIR
            project_settings = load_settings(settings_path(project_dir))
            project_models = load_models(models_path(project_dir))

        merged = deep_merge(_BUILTIN_DEFAULTS, global_settings)
        merged = deep_merge(merged, project_settings)

        providers: dict[str, Any] = {}
        providers = deep_merge(providers, global_models.get("providers") or {})
        providers = deep_merge(providers, project_models.get("providers") or {})
        if isinstance(merged.get("providers"), dict):
            providers = deep_merge(providers, merged["providers"])
        merged["providers"] = providers

        # settings 的 extensions.<name> 与 auth.json 的 extensions.<name> 深合并，auth 优先
        extensions: dict[str, Any] = dict(merged.get("extensions") or {})
        auth_extensions = global_auth.get("extensions") or {}
        if isinstance(auth_extensions, dict):
            for name, auth_section in auth_extensions.items():
                if isinstance(auth_section, dict):
                    extensions[name] = deep_merge(
                        extensions.get(name) or {},
                        auth_section,
                    )
        merged["extensions"] = extensions

        agents = scan_agents_dir(_BUILTIN_AGENTS_DIR, project_root=root)
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

        try:
            return AppConfig.model_validate(merged)
        except ValidationError as exc:
            missing = [
                e.get("loc", ("?",))[0]
                for e in exc.errors()
                if e.get("type") == "missing"
            ]
            if missing:
                raise ValueError(
                    "分层配置不完整，缺少: "
                    + ", ".join(str(m) for m in missing)
                    + f"。请写入 {resolved_home}/settings.json 与 models.json，"
                    "或执行: pickel config migrate --from config.yaml"
                ) from exc
            raise
