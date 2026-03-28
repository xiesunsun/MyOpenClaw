from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from myopenclaw.agent.behavior_loader import BehaviorLoader
from myopenclaw.agent.definition import AgentDefinition
from myopenclaw.llm.config import ModelConfig, ModelSelection, ProviderModelConfig


class ProviderCatalog(BaseModel):
    models: dict[str, ProviderModelConfig]


class AgentConfig(BaseModel):
    workspace_path: Path
    behavior_path: Path
    llm: ModelSelection | None = None


class AppConfig(BaseModel):
    root: Path = Field(default_factory=Path.cwd, exclude=True)
    default_agent: str
    default_llm: ModelSelection
    providers: dict[str, ProviderCatalog]
    agents: dict[str, AgentConfig]

    @model_validator(mode="after")
    def resolve_agent_paths(self) -> "AppConfig":
        for agent_config in self.agents.values():
            agent_config.workspace_path = self._resolve_path(
                agent_config.workspace_path
            )
            agent_config.behavior_path = self._resolve_path(agent_config.behavior_path)
        return self

    @classmethod
    def load(cls, config_path: Path) -> "AppConfig":
        config_file = (
            config_path if config_path.is_absolute() else (Path.cwd() / config_path)
        )
        if not config_file.exists():
            raise FileNotFoundError(f"Config file not found: {config_file}")
        with config_file.open(encoding="utf-8") as handle:
            config_data = yaml.safe_load(handle) or {}
        config_data["root"] = config_file.parent
        return cls.model_validate(config_data)

    def _resolve_path(self, path: Path) -> Path:
        if path.is_absolute():
            return path
        return self.root / path

    def resolve_agent_definition(self, agent_id: str | None = None) -> AgentDefinition:
        resolved_agent_id = agent_id or self.default_agent
        if resolved_agent_id not in self.agents:
            raise KeyError(f"Unknown agent: {resolved_agent_id}")

        agent_config = self.agents[resolved_agent_id]
        behavior_instruction = BehaviorLoader.load(agent_config.behavior_path)
        return AgentDefinition(
            agent_id=resolved_agent_id,
            workspace_path=agent_config.workspace_path,
            behavior_path=agent_config.behavior_path,
            behavior_instruction=behavior_instruction,
            model_config=self.resolve_model_config(agent_config.llm),
        )

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
