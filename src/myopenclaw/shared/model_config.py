from typing import Any

from pydantic import BaseModel, Field, field_validator


class BaseModelConfig(BaseModel):
    api_key: str | None = None
    api_base: str | None = None
    temperature: float | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int = 65536
    provider_options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("api_base")
    @classmethod
    def api_base_must_be_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("https://", "http://")):
            raise ValueError("api_base must be a vaild URL")
        return value


class ModelConfig(BaseModelConfig):
    provider: str
    model: str


class ModelSelection(BaseModel):
    provider: str
    model: str


class ProviderModelConfig(BaseModelConfig):
    pass
