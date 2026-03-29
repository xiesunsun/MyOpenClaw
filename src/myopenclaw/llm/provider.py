from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from myopenclaw.runtime_protocols.generation import GenerateRequest, GenerateResult

if TYPE_CHECKING:
    from myopenclaw.llm.config import ModelConfig


class BaseLLMProvider(ABC):
    @classmethod
    @abstractmethod
    def from_config(cls, config: "ModelConfig") -> "BaseLLMProvider":
        raise NotImplementedError

    @abstractmethod
    async def generate(self, request: GenerateRequest) -> GenerateResult:
        raise NotImplementedError
