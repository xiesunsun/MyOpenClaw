"""Slash 命令：/model /thinking /agent /new /reload。"""

from __future__ import annotations

import textwrap
import unittest
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from pickel.agents.agent import Agent
from pickel.app.boot import Boot
from pickel.cli.chat import ChatLoop
from pickel.config.environ import Environ
from pickel.conversations.session import Session
from pickel.runs.run import Run
from pickel.shared.model_config import ModelConfig, ModelSelection
from rich.console import Console
from tests.helpers.yaml_app_config import app_config_from_yaml_file


def _write_project(root: Path, *, agents: dict[str, str] | None = None) -> Path:
    agents = agents or {"Pickle": "You are Pickle.\n"}
    for agent_id, behavior in agents.items():
        agent_dir = root / "agents" / agent_id
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "AGENT.md").write_text(behavior, encoding="utf-8")
        (agent_dir / "agent.yaml").write_text(
            textwrap.dedent(
                f"""
                workspace_path: workspace
                behavior_path: agents/{agent_id}
                tools: []
                """
            ).strip()
            + "\n",
            encoding="utf-8",
        )
    (root / "workspace").mkdir(exist_ok=True)
    config_path = root / "config.yaml"
    agent_block = "\n".join(
        f"  {aid}:\n    workspace_path: workspace\n    behavior_path: agents/{aid}"
        for aid in agents
    )
    config_path.write_text(
        textwrap.dedent(
            """
            default_agent: Pickle
            default_llm:
              provider: anthropic
              model: claude-opus-4-7
            providers:
              anthropic:
                models:
                  claude-opus-4-7:
                    api_key: test-key
                    max_output_tokens: 1024
                    provider_options:
                      thinking: low
                  claude-sonnet-4-6:
                    api_key: test-key
                    max_output_tokens: 2048
                    provider_options:
                      thinking: medium
              google/gemini:
                models:
                  gemini-3-flash-preview:
                    api_key: gemini-key
                    temperature: 0.2
                    max_output_tokens: 512
                    provider_options: {}
            agents:
            """
        ).strip()
        + "\n"
        + agent_block
        + "\n",
        encoding="utf-8",
    )
    return config_path


class SlashCommandTests(unittest.IsolatedAsyncioTestCase):
    def _loop_from_config(
        self,
        config_path: Path,
        *,
        console: Console | None = None,
        inputs: list[str] | None = None,
    ) -> ChatLoop:
        boot = Boot.from_config(app_config_from_yaml_file(config_path))
        agent, run = boot.build_run(agent_id="Pickle")
        session = Session.create(agent_id="Pickle", session_id="sess-1")
        submitted = iter(inputs or ["/exit"])
        return ChatLoop(
            agent=agent,
            run=run,
            session=session,
            console=console or Console(file=StringIO(), force_terminal=False, width=120),
            input_reader=lambda _: next(submitted),
            boot=boot,
            app_config=boot.app_config,
        )

    async def test_model_list_and_set_preserves_case(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = _write_project(root)
            output = StringIO()
            console = Console(file=output, force_terminal=False, width=120, record=True)
            loop = self._loop_from_config(
                config_path,
                console=console,
                inputs=[
                    "/model",
                    "/model google/gemini/gemini-3-flash-preview",
                    "/exit",
                ],
            )
            with patch("pickel.runs.run.create_llm_provider", return_value=MagicMock()):
                await loop.run()

            self.assertEqual("google/gemini", loop._run.environ.llm.provider)
            self.assertEqual("gemini-3-flash-preview", loop._run.environ.llm.model)
            self.assertEqual("google/gemini", loop._run.agent.model_config.provider)
            rendered = console.export_text()
            self.assertIn("anthropic/claude-opus-4-7", rendered)
            self.assertIn("google/gemini/gemini-3-flash-preview", rendered)

    async def test_thinking_sets_environ_provider_options(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = _write_project(root)
            loop = self._loop_from_config(
                config_path,
                inputs=["/thinking xhigh", "/exit"],
            )
            with patch("pickel.runs.run.create_llm_provider", return_value=MagicMock()):
                await loop.run()
            self.assertEqual("xhigh", loop._run.environ.provider_options.get("thinking"))
            self.assertEqual(
                "xhigh",
                loop._run.agent.model_config.provider_options.get("thinking"),
            )

    async def test_agent_list_and_switch_creates_empty_session(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = _write_project(
                root,
                agents={"Pickle": "You are Pickle.\n", "Other": "You are Other.\n"},
            )
            output = StringIO()
            console = Console(file=output, force_terminal=False, width=120, record=True)
            old_session_id = "sess-old"
            boot = Boot.from_config(app_config_from_yaml_file(config_path))
            agent, run = boot.build_run(agent_id="Pickle")
            session = Session.create(agent_id="Pickle", session_id=old_session_id)
            from pickel.conversations.agent_message import UserMessage
            from pickel.conversations.content_blocks import TextContent

            session.append_user(UserMessage(content=[TextContent(text="old")]))
            submitted = iter(["/agent", "/agent Other", "/exit"])

            started: list[dict] = []

            class FakeSessionService:
                def start(self, *, agent_id: str, cwd: str | None = None):
                    started.append({"agent_id": agent_id, "cwd": cwd})
                    return Session.create(agent_id=agent_id, session_id="sess-new")

                def build_preview(self, *, session: Session):
                    from pickel.conversations.session_preview import SessionPreview

                    return SessionPreview(
                        session_id=session.session_id,
                        agent_id=session.agent_id,
                        created_at=session.created_at,
                        updated_at=session.updated_at,
                        status=session.status,
                        message_count=len(session.entries),
                        last_message=None,
                    )

                def close(self, *, session: Session) -> None:
                    pass

                def flush_new_entries(self, *, session, entries) -> None:
                    pass

            fake_ss = FakeSessionService()

            def build_ss(*, agent_id=None):
                return fake_ss

            def build_run(*, agent_id=None, session_service=None):
                a, r = boot.build_run(agent_id=agent_id, session_service=session_service)
                return a, r

            boot.build_session_service = build_ss  # type: ignore[method-assign]
            loop = ChatLoop(
                agent=agent,
                run=run,
                session=session,
                console=console,
                input_reader=lambda _: next(submitted),
                session_service=fake_ss,
                boot=boot,
                app_config=boot.app_config,
            )
            with patch("pickel.runs.run.create_llm_provider", return_value=MagicMock()):
                await loop.run()

            rendered = console.export_text()
            self.assertIn("Other", rendered)
            self.assertIn("use /agent <id>", rendered)
            self.assertEqual("Other", loop.agent_id)
            self.assertEqual("sess-new", loop.session.session_id)
            self.assertEqual(0, len(loop.session.entries))
            self.assertEqual(old_session_id, session.session_id)  # 旧 session 未改 id
            self.assertEqual(1, len(session.entries))  # 旧历史仍在旧对象

    async def test_new_keeps_run_and_agent(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = _write_project(root)
            boot = Boot.from_config(app_config_from_yaml_file(config_path))
            agent, run = boot.build_run(agent_id="Pickle")
            session = Session.create(agent_id="Pickle", session_id="sess-1")
            from pickel.conversations.agent_message import UserMessage
            from pickel.conversations.content_blocks import TextContent

            session.append_user(UserMessage(content=[TextContent(text="hi")]))
            started: list[str] = []

            class FakeSessionService:
                def start(self, *, agent_id: str, cwd: str | None = None):
                    started.append(agent_id)
                    return Session.create(agent_id=agent_id, session_id="sess-2")

                def close(self, *, session: Session) -> None:
                    pass

                def flush_new_entries(self, *, session, entries) -> None:
                    pass

            fake_ss = FakeSessionService()
            submitted = iter(["/new", "/exit"])
            loop = ChatLoop(
                agent=agent,
                run=run,
                session=session,
                console=Console(file=StringIO(), force_terminal=False),
                input_reader=lambda _: next(submitted),
                session_service=fake_ss,
                boot=boot,
                app_config=boot.app_config,
            )
            await loop.run()
            self.assertIs(run, loop._run)
            self.assertEqual("Pickle", loop.agent_id)
            self.assertEqual("sess-2", loop.session.session_id)
            self.assertEqual(0, len(loop.session.entries))
            self.assertEqual(["Pickle"], started)

    async def test_reload_preserves_environ_and_session(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = _write_project(root)
            boot = Boot.from_config(app_config_from_yaml_file(config_path))
            agent, run = boot.build_run(agent_id="Pickle")
            run.environ = Environ(
                llm=ModelSelection(
                    provider="google/gemini", model="gemini-3-flash-preview"
                ),
                provider_options={"thinking": "xhigh"},
            )
            with patch("pickel.runs.run.create_llm_provider", return_value=MagicMock()):
                run.apply_environ_model(boot.app_config)

            session = Session.create(agent_id="Pickle", session_id="sess-keep")
            submitted = iter(["/reload", "/exit"])
            loop = ChatLoop(
                agent=agent,
                run=run,
                session=session,
                console=Console(file=StringIO(), force_terminal=False, width=120, record=True),
                input_reader=lambda _: next(submitted),
                boot=boot,
                app_config=boot.app_config,
            )
            with (
                patch("pickel.cli.chat.Config.load", return_value=boot.app_config),
                patch("pickel.cli.chat.Boot.from_config", return_value=boot),
                patch("pickel.runs.run.create_llm_provider", return_value=MagicMock()),
            ):
                await loop.run()

            self.assertEqual("sess-keep", loop.session.session_id)
            self.assertEqual("Pickle", loop.agent_id)
            self.assertEqual(
                ModelSelection(
                    provider="google/gemini", model="gemini-3-flash-preview"
                ),
                loop._run.environ.llm,
            )
            self.assertEqual("xhigh", loop._run.environ.provider_options.get("thinking"))
            self.assertEqual(
                "google/gemini", loop._run.agent.model_config.provider
            )

    async def test_command_args_not_lowercased(self) -> None:
        """_handle_command 不得把参数整体 lower。"""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config_path = _write_project(
                root,
                agents={"Pickle": "p\n", "CamelCaseAgent": "c\n"},
            )
            boot = Boot.from_config(app_config_from_yaml_file(config_path))
            agent, run = boot.build_run(agent_id="Pickle")
            session = Session.create(agent_id="Pickle")

            class FakeSessionService:
                def start(self, *, agent_id: str, cwd: str | None = None):
                    return Session.create(agent_id=agent_id, session_id="n1")

                def close(self, *, session: Session) -> None:
                    pass

            loop = ChatLoop(
                agent=agent,
                run=run,
                session=session,
                console=Console(file=StringIO(), force_terminal=False),
                session_service=FakeSessionService(),
                boot=boot,
                app_config=boot.app_config,
            )
            with patch("pickel.runs.run.create_llm_provider", return_value=MagicMock()):
                await loop._handle_command("/agent CamelCaseAgent")
            self.assertEqual("CamelCaseAgent", loop.agent_id)


if __name__ == "__main__":
    unittest.main()
