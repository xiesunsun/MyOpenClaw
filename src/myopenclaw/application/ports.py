from __future__ import annotations

from abc import ABC, abstractmethod

from myopenclaw.application.contracts import GenerateRequest, GenerateResult, ToolSpec
from myopenclaw.domain.agent import Agent
from myopenclaw.domain.message import ToolCall, ToolCallResult


class LLMPort(ABC):
    @abstractmethod
    async def generate(self, request: GenerateRequest) -> GenerateResult:
        raise NotImplementedError


class ToolExecutorPort(ABC):
    @abstractmethod
    def describe_tools(self, tool_ids: list[str]) -> list[ToolSpec]:
        raise NotImplementedError

    @abstractmethod
    async def execute_calls(
        self,
        *,
        tool_calls: list[ToolCall],
        agent: Agent,
        session_id: str,
    ) -> list[ToolCallResult]:
        raise NotImplementedError
