import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

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
        config_file = (
            config_path if config_path.is_absolute() else (Path.cwd() / config_path)
        )
        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_file}")
        with config_file.open(encoding="utf-8") as handle:
            config_data = cls._expand_env_vars(yaml.safe_load(handle) or {})
        config_data["root"] = config_file.parent
        return cls.model_validate(config_data)

    @classmethod
    def _expand_env_vars(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: cls._expand_env_vars(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._expand_env_vars(item) for item in value]
        if isinstance(value, str):
            return _ENV_VAR_PATTERN.sub(cls._replace_env_var, value)
        return value

    @staticmethod
    def _replace_env_var(match: re.Match[str]) -> str:
        env_name = match.group(1)
        env_value = os.environ.get(env_name)
        if env_value is None:
            raise ValueError(f"Environment variable '{env_name}' is not set")
        return env_value

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
        self, selection: ModelSelection | None = None
    ) -> ModelConfig:
        resolved_selection = selection or self.default_llm
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
        return ModelConfig.model_validate(data)

    def resolve_file_access_mode(
        self, agent_id: str | None = None
    ) -> FileAccessMode:
        agent_config = self.get_agent_config(agent_id)
        return agent_config.file_access_mode or self.default_file_access_mode

    def resolve_skills_path(self, agent_id: str | None = None) -> Path | None:
        agent_config = self.get_agent_config(agent_id)
        return agent_config.skills_path or self.default_skills_path


_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
