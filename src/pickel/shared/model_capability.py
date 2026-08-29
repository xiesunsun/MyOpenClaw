"""模型能力 Profile 的配置值对象。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelCapabilityProfile(BaseModel):
    """描述模型的稳定语义能力，不承载具体协议字段。"""

    model_config = ConfigDict(frozen=True)

    name: str = "generic"
    reasoning_supported: bool = False
    reasoning_levels: tuple[str, ...] = ()
    reasoning_default: str | None = None
    thinking_enabled: bool = False
    clear_thinking: bool | None = None
    preserve_reasoning_after_tool_call: bool = False
    tool_call_streaming: bool = False
    tool_call_argument_deltas: bool = False
    parallel_tool_calls: bool | None = None
    input_modalities: tuple[str, ...] = Field(default=("text",))
    max_context_window_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_reasoning(self) -> "ModelCapabilityProfile":
        levels = tuple(level.strip() for level in self.reasoning_levels)
        if any(not level for level in levels):
            raise ValueError("reasoning_levels 不能包含空值")
        if len(set(levels)) != len(levels):
            raise ValueError("reasoning_levels 不能包含重复值")
        if self.reasoning_supported and not levels:
            raise ValueError("reasoning_supported=true 时必须声明 reasoning_levels")
        if self.reasoning_default is not None and self.reasoning_default not in levels:
            raise ValueError("reasoning_default 必须属于 reasoning_levels")
        if not self.reasoning_supported and (
            levels or self.reasoning_default is not None
        ):
            raise ValueError(
                "reasoning_supported=false 时不能声明 reasoning_levels/reasoning_default"
            )
        if self.tool_call_argument_deltas and not self.tool_call_streaming:
            raise ValueError(
                "tool_call_argument_deltas=true 时必须启用 tool_call_streaming"
            )
        modalities = tuple(modality.strip() for modality in self.input_modalities)
        if any(not modality for modality in modalities):
            raise ValueError("input_modalities 不能包含空值")
        object.__setattr__(self, "reasoning_levels", levels)
        object.__setattr__(self, "input_modalities", modalities)
        return self


GLM_5_3_FLASH_CAPABILITY_PROFILE = ModelCapabilityProfile(
    name="glm-5.3-flash",
    reasoning_supported=True,
    reasoning_levels=("low", "high", "max"),
    reasoning_default="max",
    thinking_enabled=True,
    clear_thinking=False,
    preserve_reasoning_after_tool_call=True,
    tool_call_streaming=True,
    tool_call_argument_deltas=True,
    parallel_tool_calls=False,
    input_modalities=("text", "image", "video", "file"),
    max_context_window_tokens=1_000_000,
    max_output_tokens=131_072,
)
