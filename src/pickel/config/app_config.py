import os
import re
from pathlib import Path
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from pickel.config.environ import Environ
from pickel.shared.file_access import FileAccessMode
from pickel.shared.model_config import (
    ModelConfig,
    ModelSelection,
    ProviderModelConfig,
)

_DEFAULT_WIRE_PROTOCOLS = {
    "anthropic": "anthropic-messages",
    "openai": "openai-responses",
    "google/gemini": "gemini-generate-content",
}
_PROVIDER_DEFAULT_API_BASES = {
    "opencode-go": "https://opencode.ai/zen/go/v1",
}
from pickel.tools.sandbox import SandboxSettings


class ProviderCatalog(BaseModel):
    models: dict[str, ProviderModelConfig]


class AgentModels(BaseModel):
    primary: ModelSelection | None = None
    worker: ModelSelection | None = None
    utility: ModelSelection | None = None


class AgentConfig(BaseModel):
    workspace_path: Path
    behavior_path: Path
    models: AgentModels = Field(default_factory=AgentModels)
    tools: list[str] = Field(default_factory=list)
    # "*" 保持旧 Agent 的全量装配语义；新 Agent 应显式声明运行所需 Extension。
    extensions: list[str] = Field(default_factory=lambda: ["*"])
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


class SkillSettings(BaseModel):
    write_approval: bool = True
    guard: bool = True


class TraceSettings(BaseModel):
    """派生运行轨迹；默认 standard，写入失败不影响 Runtime。"""

    mode: Literal["off", "standard", "full"] = "standard"
    queue_capacity: int = Field(default=8192, ge=1)
    batch_size: int = Field(default=128, ge=1)
    flush_interval_ms: int = Field(default=250, ge=10)
    max_file_size_mb: int = Field(default=64, ge=1)
    max_age_days: int = Field(default=14, ge=0)
    max_total_size_mb: int = Field(default=1024, ge=1)


class ObservabilitySettings(BaseModel):
    trace: TraceSettings = Field(default_factory=TraceSettings)


class AppConfig(BaseModel):
    root: Path = Field(default_factory=Path.cwd, exclude=True)
    default_agent: str
    default_llm: ModelSelection
    default_file_access_mode: FileAccessMode = FileAccessMode.WORKSPACE
    default_skills_path: Path | None = None
    react_max_steps: int = 8
    context_cli_turn_window: int = 5
    model_request_max_attempts: int = Field(default=3, ge=1)
    model_request_retry_initial_delay_ms: int = Field(default=1000, ge=0)
    model_request_retry_max_delay_ms: int = Field(default=4000, ge=0)
    max_parallel_model_requests: int = Field(default=2, ge=1)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    providers: dict[str, ProviderCatalog]
    agents: dict[str, AgentConfig]
    # extension 的原始配置段：core 不认识任何 extension 的配置模型，
    # 解析由 extension 自己做（ExtensionHost.config）
    extensions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # 进程级沙箱：Linux Bubblewrap / macOS Seatbelt；strict 时缺依赖即拒绝
    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)
    # skill 自管理（V1a）：写入默认待审、内容护栏默认开
    skills: SkillSettings = Field(default_factory=SkillSettings)
    # auth.json 中 providers 级密钥；resolve_model_config 在模型缺字段时回填
    auth_providers: dict[str, dict[str, Any]] = Field(
        default_factory=dict, exclude=True
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_trace_enabled(cls, value: Any) -> Any:
        """运行时兼容旧 settings.trace_enabled，不要求用户先迁移配置。"""
        if not isinstance(value, dict) or "trace_enabled" not in value:
            return value
        migrated = dict(value)
        enabled = bool(migrated.pop("trace_enabled"))
        observability = dict(migrated.get("observability") or {})
        trace = dict(observability.get("trace") or {})
        trace["mode"] = "standard" if enabled else "off"
        observability["trace"] = trace
        migrated["observability"] = observability
        return migrated

    @model_validator(mode="after")
    def resolve_agent_paths(self) -> "AppConfig":
        if (
            self.model_request_retry_max_delay_ms
            < self.model_request_retry_initial_delay_ms
        ):
            raise ValueError("模型请求重试上限不能小于初始延迟")
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
        if data.get("wire_protocol") is None:
            data["wire_protocol"] = _DEFAULT_WIRE_PROTOCOLS.get(
                resolved_selection.provider
            )
        if data.get("wire_protocol") is None:
            raise ValueError(
                f"Provider '{resolved_selection.provider}' 的模型 "
                f"'{resolved_selection.model}' 必须显式声明 wire_protocol"
            )
        # 模型缺 api_key/api_base 时用 auth.providers 回填
        auth_entry = self.auth_providers.get(resolved_selection.provider) or {}
        if data.get("api_key") is None and auth_entry.get("api_key") is not None:
            data["api_key"] = auth_entry["api_key"]
        if data.get("api_base") is None and auth_entry.get("api_base") is not None:
            data["api_base"] = auth_entry["api_base"]
        if data.get("api_base") is None:
            data["api_base"] = _PROVIDER_DEFAULT_API_BASES.get(
                resolved_selection.provider
            )
        if resolved_selection.provider == "opencode-go" and not data.get("api_key"):
            raise ValueError("OpenCode Go 需要 providers.opencode-go.api_key")
        model = ModelConfig.model_validate(data)
        if environ is not None:
            model = environ.overlay_model_config(model)
        return model

    def resolve_file_access_mode(self, agent_id: str | None = None) -> FileAccessMode:
        agent_config = self.get_agent_config(agent_id)
        return agent_config.file_access_mode or self.default_file_access_mode

    def resolve_skills_path(self, agent_id: str | None = None) -> Path | None:
        agent_config = self.get_agent_config(agent_id)
        return agent_config.skills_path or self.default_skills_path

    def resolve_agent_extensions(
        self,
        agent_ids: Iterable[str] | None = None,
    ) -> frozenset[str] | None:
        """解析目标 Agent 所需 Extension；None 表示装载全部。"""
        resolved_ids = tuple(agent_ids) if agent_ids is not None else tuple(self.agents)
        names: set[str] = set()
        for agent_id in resolved_ids:
            agent_config = self.get_agent_config(agent_id)
            if "*" in agent_config.extensions:
                return None
            names.update(agent_config.extensions)
        return frozenset(names)


_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
