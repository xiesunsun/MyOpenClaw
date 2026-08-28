from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, Field, field_validator, model_validator

WireProtocol: TypeAlias = Literal[
    "openai-responses",
    "openai-chat-completions",
    "anthropic-messages",
    "gemini-generate-content",
]


class BaseModelConfig(BaseModel):
    wire_protocol: WireProtocol | None = None
    api_key: str | None = None
    api_base: str | None = None
    temperature: float | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int = 65536
    # 总上下文窗口（输入与输出之和）；与供应商独立输入上限不是同一概念。
    context_window_tokens: int | None = Field(default=None, ge=1)
    provider_options: dict[str, Any] = Field(default_factory=dict)

    def effective_input_token_limit(
        self, requested_output_tokens: int | None = None
    ) -> int | None:
        """按总窗口和本次输出预留推导可用输入容量。

        ``max_input_tokens`` 只有在供应商明确给出独立输入硬上限时才填写。
        未知时仍可使用 ``context_window_tokens - output_reserve``，但不能把
        总窗口本身误当成输入上限。
        """
        reserve = (
            self.max_output_tokens
            if requested_output_tokens is None
            else requested_output_tokens
        )
        if reserve < 0:
            raise ValueError("requested_output_tokens 不能小于 0")
        if self.context_window_tokens is None:
            return self.max_input_tokens
        context_input = max(0, self.context_window_tokens - reserve)
        if self.max_input_tokens is None:
            return context_input
        return min(self.max_input_tokens, context_input)

    @field_validator("api_base")
    @classmethod
    def api_base_must_be_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("https://", "http://")):
            raise ValueError("api_base must be a vaild URL")
        return value

    @model_validator(mode="after")
    def context_window_must_fit_output_budget(self) -> "BaseModelConfig":
        if (
            self.context_window_tokens is not None
            and self.context_window_tokens <= self.max_output_tokens
        ):
            raise ValueError("context_window_tokens 必须大于 max_output_tokens")
        return self


class ModelConfig(BaseModelConfig):
    provider: str
    model: str
    wire_protocol: WireProtocol


class ModelSelection(BaseModel):
    provider: str
    model: str


class ProviderModelConfig(BaseModelConfig):
    pass
