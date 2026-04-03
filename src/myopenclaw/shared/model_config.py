from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class BaseModelConfig(BaseModel):
    api_key: str | None = None
    api_base: str | None = None
    temperature: float = 1.0
    max_output_tokens: int = 65536
    thinking_level: str | None = None
    provider_options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("api_base")
    @classmethod
    def api_base_must_be_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("https://", "http://")):
            raise ValueError("api_base must be a vaild URL")
        return value

    @model_validator(mode="after")
    def merge_legacy_provider_fields(self) -> "BaseModelConfig":
        if self.thinking_level and "thinking_level" not in self.provider_options:
            self.provider_options["thinking_level"] = self.thinking_level
        if self.thinking_level is None:
            thinking_level = self.provider_options.get("thinking_level")
            if isinstance(thinking_level, str):
                self.thinking_level = thinking_level
        return self


class ModelConfig(BaseModelConfig):
    provider: str
    model: str


class ModelSelection(BaseModel):
    provider: str
    model: str


class ProviderModelConfig(BaseModelConfig):
    pass
