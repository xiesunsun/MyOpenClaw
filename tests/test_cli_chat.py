import tempfile
import textwrap
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import AsyncMock, Mock, patch

from rich.console import Console

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from myopenclaw.agent.agent import Agent
from myopenclaw.agent.definition import AgentDefinition
from myopenclaw.app.bootstrap import LoadedAgentRuntime
from myopenclaw.config.app_config import AppConfig
from myopenclaw.interfaces.cli.chat import ChatLoop
from myopenclaw.llm.config import ModelConfig
from myopenclaw.llm.provider import BaseLLMProvider, ChatRequest, ChatResult


class FakeProvider(BaseLLMProvider):
    @classmethod
    def from_config(cls, config: ModelConfig) -> "FakeProvider":
        return cls()

    async def chat(self, request: ChatRequest) -> ChatResult:
        return ChatResult(text=f"reply:{request.messages[-1].text}")


class ChatLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_from_config_path_builds_session_and_handles_user_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = root / "config.yaml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    default_agent: Pickle

                    default_llm:
                      provider: google/gemini
                      model: gemini-3-flash-preview

                    providers:
                      google/gemini:
                        models:
                          gemini-3-flash-preview:
                            temperature: 1.0
                            provider_options: {}

                    agents:
                      Pickle:
                        workspace_path: pickle_workspace
                        behavior_path: agents/Pickle
                        llm:
                          provider: google/gemini
                          model: gemini-3-flash-preview
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )
            agent_dir = root / "agents" / "Pickle"
            agent_dir.mkdir(parents=True)
            (agent_dir / "AGENT.md").write_text(
                textwrap.dedent(
                    """
                    You are Pickle.
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            with patch("myopenclaw.agent.agent.create_llm_provider", return_value=FakeProvider()):
                loop = ChatLoop.from_config_path(config_path=config_path, agent_id=None)
                reply = await loop.handle_user_input("hello")

        self.assertEqual(reply, "reply:hello")
        self.assertEqual(loop.session.state.messages[-1].text, "reply:hello")

    def test_from_config_path_uses_agent_loader(self) -> None:
        config_path = Path("/tmp/workspace/config.yaml")
        app_config = AppConfig(
            root=config_path.parent,
            default_agent="default",
            default_llm={"provider": "google/gemini", "model": "gemini-3-flash-preview"},
            providers={
                "google/gemini": {
                    "models": {
                        "gemini-3-flash-preview": {
                            "provider_options": {},
                        }
                    }
                }
            },
            agents={
                "default": {
                    "workspace_path": "/tmp/workspace",
                    "behavior_path": "/tmp/workspace/agents/default",
                }
            },
        )
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
            provider=FakeProvider(),
        )
        loaded_runtime = LoadedAgentRuntime(
            app_config=app_config,
            agent_id="default",
            agent=agent,
        )

        with patch(
            "myopenclaw.interfaces.cli.chat.AppBootstrap.from_config_path",
            return_value=loaded_runtime,
        ) as loader:
            loop = ChatLoop.from_config_path(config_path=config_path, agent_id=None)

        loader.assert_called_once_with(config_path=config_path, agent_id=None)
        self.assertIs(loop.agent, agent)

    async def test_run_renders_header_help_and_exit_without_calling_agent(self) -> None:
        session = SimpleNamespace(send_user_message=AsyncMock())
        output = StringIO()
        loop = ChatLoop(
            agent=Mock(definition=SimpleNamespace(agent_id="Pickle")),
            agent_id="Pickle",
            session=session,
            config_path=Path("/tmp/workspace/config.yaml"),
            console=Console(file=output, force_terminal=False, width=100),
            input_reader=Mock(side_effect=["/help", "/exit"]),
        )

        await loop.run()

        rendered = output.getvalue()
        self.assertIn("MyOpenClaw Chat", rendered)
        self.assertIn("Agent: Pickle", rendered)
        self.assertIn("/help", rendered)
        self.assertIn("Available commands", rendered)
        session.send_user_message.assert_not_awaited()

    async def test_run_renders_chat_reply_and_session_summary(self) -> None:
        session = SimpleNamespace(send_user_message=AsyncMock(return_value="reply:hello"))
        output = StringIO()
        loop = ChatLoop(
            agent=Mock(definition=SimpleNamespace(agent_id="Pickle")),
            agent_id="Pickle",
            session=session,
            config_path=Path("/tmp/workspace/config.yaml"),
            console=Console(file=output, force_terminal=False, width=100),
            input_reader=Mock(side_effect=["hello", "/session", "/exit"]),
        )

        await loop.run()

        rendered = output.getvalue()
        self.assertIn("You", rendered)
        self.assertIn("hello", rendered)
        self.assertIn("Pickle", rendered)
        self.assertIn("reply:hello", rendered)
        self.assertIn("Messages: 2", rendered)

    async def test_run_redraws_header_after_clear_command(self) -> None:
        output = StringIO()
        loop = ChatLoop(
            agent=Mock(definition=SimpleNamespace(agent_id="Pickle")),
            agent_id="Pickle",
            session=SimpleNamespace(send_user_message=AsyncMock()),
            console=Console(file=output, force_terminal=False, width=100),
            input_reader=Mock(side_effect=["/clear", "/exit"]),
        )

        await loop.run()

        self.assertGreaterEqual(output.getvalue().count("MyOpenClaw Chat"), 2)

    async def test_run_handles_keyboard_interrupt_gracefully(self) -> None:
        output = StringIO()
        loop = ChatLoop(
            agent=Mock(definition=SimpleNamespace(agent_id="Pickle")),
            agent_id="Pickle",
            session=SimpleNamespace(send_user_message=AsyncMock()),
            console=Console(file=output, force_terminal=False, width=100),
            input_reader=Mock(side_effect=KeyboardInterrupt()),
        )

        await loop.run()

        self.assertIn("Session closed", output.getvalue())
