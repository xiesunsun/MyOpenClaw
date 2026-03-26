import unittest
from pathlib import Path
import sys

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from myopenclaw.agent.agent import Agent
from myopenclaw.agent.definition import AgentDefinition
from myopenclaw.conversation.message import MessageRole
from myopenclaw.conversation.session import AgentSession
from myopenclaw.llm.config import ModelConfig
from myopenclaw.llm.provider import BaseLLMProvider, ChatRequest, ChatResult


class FakeProvider(BaseLLMProvider):
    def __init__(self) -> None:
        self.requests: list[ChatRequest] = []

    @classmethod
    def from_config(cls, config: ModelConfig) -> "FakeProvider":
        return cls()

    async def chat(self, request: ChatRequest) -> ChatResult:
        self.requests.append(request)
        last_user_message = request.messages[-1].text
        return ChatResult(text=f"echo:{last_user_message}")


class AgentSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_session_passes_system_instruction_separately(self) -> None:
        provider = FakeProvider()
        agent = Agent(
            definition=AgentDefinition(
                agent_id="default",
                workspace_path=Path("/tmp/workspace"),
                behavior_path=Path("/tmp/workspace/agents/default"),
                behavior_instruction="You are a helpful assistant.",
                model_config=ModelConfig(
                    provider="google/gemini",
                    model="gemini-3-flash-preview",
                ),
            ),
            provider=provider,
        )

        session = agent.new_session("session-1")
        reply = await session.send_user_message("hello")

        self.assertIsInstance(session, AgentSession)
        self.assertEqual(reply, "echo:hello")
        self.assertEqual(provider.requests[0].system_instruction, "You are a helpful assistant.")
        self.assertEqual(
            [(message.role, message.text) for message in session.state.messages],
            [
                (MessageRole.USER, "hello"),
                (MessageRole.ASSISTANT, "echo:hello"),
            ],
        )
