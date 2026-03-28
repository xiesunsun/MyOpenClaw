from dataclasses import dataclass
import time
from typing import TYPE_CHECKING

from myopenclaw.conversation.metadata import MessageMetadata
from myopenclaw.conversation.state import SessionState
from myopenclaw.llm import ChatRequest
from myopenclaw.llm.provider import ChatResult

if TYPE_CHECKING:
    from myopenclaw.agent.agent import Agent


@dataclass
class AgentSession:
    agent: "Agent"
    state: SessionState

    async def send_user_message(self, text: str) -> ChatResult:
        self.state.add_user_message(text)
        start = time.perf_counter()
        result = await self.agent.provider.chat(
            ChatRequest(
                system_instruction=self.agent.system_instruction or None,
                messages=list(self.state.messages),
            )
        )
        elapsed_ms = round((time.perf_counter() - start) * 1000)
        metadata = result.metadata or MessageMetadata(
            provider=self.agent.definition.model_config.provider,
            model=self.agent.definition.model_config.model,
            input_tokens=result.usage.input_tokens if result.usage else None,
            output_tokens=result.usage.output_tokens if result.usage else None,
            elapsed_ms=elapsed_ms,
        )
        result.metadata = metadata
        self.state.add_assistant_message(result.text, metadata=metadata)
        return result
