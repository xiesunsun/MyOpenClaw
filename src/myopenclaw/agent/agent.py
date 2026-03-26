from typing import TYPE_CHECKING
from uuid import uuid4

from myopenclaw.conversation.session import AgentSession
from myopenclaw.conversation.state import SessionState
from myopenclaw.llm import BaseLLMProvider, create_llm_provider

if TYPE_CHECKING:
    from myopenclaw.agent.definition import AgentDefinition


class Agent:
    def __init__(
        self,
        definition: "AgentDefinition",
        provider: BaseLLMProvider | None = None,
    ) -> None:
        self.definition = definition
        self.provider = provider or create_llm_provider(definition.model_config)

    @property
    def system_instruction(self) -> str:
        return self.definition.behavior_instruction

    @property
    def workspace(self):
        return self.definition.workspace_path

    def new_session(self, session_id: str | None = None) -> AgentSession:
        state = SessionState(session_id=session_id or str(uuid4()))
        return AgentSession(agent=self, state=state)
