"""Environ：进程运行态覆盖（内存，默认不落盘）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pickel.shared.model_config import ModelConfig, ModelSelection


@dataclass
class Environ:
    """对齐 Unix process environment：属进程，不属于 Session。"""

    llm: ModelSelection | None = None  # provider+model 覆盖
    provider_options: dict[str, Any] = field(default_factory=dict)  # e.g. thinking

    def apply_to_selection(self, base: ModelSelection) -> ModelSelection:
        """Environ.llm 优先，否则沿用 base。"""
        return self.llm if self.llm is not None else base

    def merge_provider_options(self, base: dict[str, Any]) -> dict[str, Any]:
        """catalog 默认 << Environ 覆盖。"""
        return {**base, **self.provider_options}

    def overlay_model_config(self, model: ModelConfig) -> ModelConfig:
        """把 provider_options 叠到已解析的 ModelConfig。"""
        if not self.provider_options:
            return model
        return model.model_copy(
            update={
                "provider_options": self.merge_provider_options(model.provider_options)
            }
        )
