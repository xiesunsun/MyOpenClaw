import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from myopenclaw.config.environ import Environ
from myopenclaw.integrations.openviking.config import OpenVikingConfig
from myopenclaw.shared.file_access import FileAccessMode
from myopenclaw.shared.model_config import (
    ModelConfig,
    ModelSelection,
    ProviderModelConfig,
)


class ProviderCatalog(BaseModel):
    models: dict[str, ProviderModelConfig]


class AgentConfig(BaseModel):
    workspace_path: Path
    behavior_path: Path
    llm: ModelSelection | None = None
    tools: list[str] = Field(default_factory=list)
    file_access_mode: FileAccessMode | None = None
    skills_path: Path | None = None
    remote_agent_id: str | None = None


def expand_env_vars(value: Any) -> Any:
    """递归展开字符串中的 ${ENV}；变量缺失时抛 ValueError。供 loader 与 yaml 加载共用。"""
    if isinstance(value, dict):
        return {key: expand_env_vars(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env_vars(item) for item in value]
    if isinstance(value, str):
        return _ENV_VAR_PATTERN.sub(_replace_env_var, value)
    return value


def _replace_env_var(match: re.Match[str]) -> str:
    env_name = match.group(1)
    env_value = os.environ.get(env_name)
    if env_value is None:
        raise ValueError(f"Environment variable '{env_name}' is not set")
    return env_value


class AppConfig(BaseModel):
    root: Path = Field(default_factory=Path.cwd, exclude=True)
    default_agent: str
    default_llm: ModelSelection
    default_file_access_mode: FileAccessMode = FileAccessMode.WORKSPACE
    default_skills_path: Path | None = None
    react_max_steps: int = 8
    context_cli_turn_window: int = 5
    providers: dict[str, ProviderCatalog]
    agents: dict[str, AgentConfig]
    openviking: OpenVikingConfig | None = None
    # auth.json 中 providers 级密钥；resolve_model_config 在模型缺字段时回填
    auth_providers: dict[str, dict[str, Any]] = Field(
        default_factory=dict, exclude=True
    )

    @model_validator(mode="after")
    def resolve_agent_paths(self) -> "AppConfig":
        if self.default_skills_path is not None:
            self.default_skills_path = self._resolve_path(self.default_skills_path)
        for agent_config in self.agents.values():
            agent_config.workspace_path = self._resolve_path(
                agent_config.workspace_path
            )
            agent_config.behavior_path = self._resolve_path(agent_config.behavior_path)
            if agent_config.skills_path is not None:
                agent_config.skills_path = self._resolve_path(agent_config.skills_path)
        return self

    @classmethod
    def load(cls, config_path: Path) -> "AppConfig":
        """从单体 config.yaml 加载。

        P0 过渡接口：共享 expand_env_vars 与 model_validate。
        新路径请用 myopenclaw.config.loader.Config.load。
        """
        config_file = (
            config_path if config_path.is_absolute() else (Path.cwd() / config_path)
        )
        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_file}")
        with config_file.open(encoding="utf-8") as handle:
            config_data = expand_env_vars(yaml.safe_load(handle) or {})
        config_data["root"] = config_file.parent
        return cls.model_validate(config_data)

    # 兼容旧私有方法名
    @classmethod
    def _expand_env_vars(cls, value: Any) -> Any:
        return expand_env_vars(value)

    @staticmethod
    def _replace_env_var(match: re.Match[str]) -> str:
        return _replace_env_var(match)

    def _resolve_path(self, path: Path) -> Path:
        if path.is_absolute():
            return path
        return self.root / path

    def get_agent_config(self, agent_id: str | None = None) -> AgentConfig:
        resolved_agent_id = agent_id or self.default_agent
        if resolved_agent_id not in self.agents:
            raise KeyError(f"Unknown agent: {resolved_agent_id}")
        return self.agents[resolved_agent_id]

    def resolve_model_config(
        self,
        selection: ModelSelection | None = None,
        *,
        environ: Environ | None = None,
    ) -> ModelConfig:
        """解析 ModelConfig。

        selection 优先级：environ.llm > selection 参数 > default_llm。
        provider_options：catalog 默认 << environ.provider_options。
        """
        base_selection = selection or self.default_llm
        resolved_selection = (
            environ.apply_to_selection(base_selection)
            if environ is not None
            else base_selection
        )
        provider_catalog = self.providers.get(resolved_selection.provider)
        if provider_catalog is None:
            raise KeyError(f"Unknown provider: {resolved_selection.provider}")
        provider_model = provider_catalog.models.get(resolved_selection.model)
        if provider_model is None:
            raise KeyError(
                f"Unknown model '{resolved_selection.model}' for provider '{resolved_selection.provider}'"
            )
        data = provider_model.model_dump()
        data["provider"] = resolved_selection.provider
        data["model"] = resolved_selection.model
        # 模型缺 api_key/api_base 时用 auth.providers 回填
        auth_entry = self.auth_providers.get(resolved_selection.provider) or {}
        if data.get("api_key") is None and auth_entry.get("api_key") is not None:
            data["api_key"] = auth_entry["api_key"]
        if data.get("api_base") is None and auth_entry.get("api_base") is not None:
            data["api_base"] = auth_entry["api_base"]
        model = ModelConfig.model_validate(data)
        if environ is not None:
            model = environ.overlay_model_config(model)
        return model

    def resolve_file_access_mode(
        self, agent_id: str | None = None
    ) -> FileAccessMode:
        agent_config = self.get_agent_config(agent_id)
        return agent_config.file_access_mode or self.default_file_access_mode

    def resolve_skills_path(self, agent_id: str | None = None) -> Path | None:
        agent_config = self.get_agent_config(agent_id)
        return agent_config.skills_path or self.default_skills_path


_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
