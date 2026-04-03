from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from myopenclaw.shared.generation import GenerateRequest, GenerateResult

if TYPE_CHECKING:
    from myopenclaw.shared.model_config import ModelConfig


class BaseLLMProvider(ABC):
    @classmethod
    @abstractmethod
    def from_config(cls, config: "ModelConfig") -> "BaseLLMProvider":
        raise NotImplementedError

    @abstractmethod
    async def generate(self, request: GenerateRequest) -> GenerateResult:
        raise NotImplementedError
