from dataclasses import dataclass
from typing import TYPE_CHECKING

from myopenclaw.conversation.state import SessionState
from myopenclaw.llm import ChatRequest

if TYPE_CHECKING:
    from myopenclaw.agent.agent import Agent


@dataclass
class AgentSession:
    agent: "Agent"
    state: SessionState

    async def send_user_message(self, text: str) -> str:
        self.state.add_user_message(text)
        result = await self.agent.provider.chat(
            ChatRequest(
                system_instruction=self.agent.system_instruction or None,
                messages=list(self.state.messages),
            )
        )
        self.state.add_assistant_message(result.text)
        return result.text
