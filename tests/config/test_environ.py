"""Environ 进程覆盖单元测试。"""

from __future__ import annotations

import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from myopenclaw.agents.agent import Agent
from myopenclaw.config.app_config import AppConfig
from myopenclaw.config.environ import Environ
from myopenclaw.runs.run import Run
from myopenclaw.shared.model_config import ModelConfig, ModelSelection


def _write_multi_model_config(root: Path) -> Path:
    config_path = root / "config.yaml"
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
              Pickle:
                workspace_path: workspace
                behavior_path: agents/Pickle
            """
        ).strip(),
        encoding="utf-8",
    )
    return config_path


class EnvironTests(unittest.TestCase):
    def test_apply_to_selection_overrides_when_llm_set(self) -> None:
        base = ModelSelection(provider="anthropic", model="claude-opus-4-7")
        env = Environ(
            llm=ModelSelection(provider="google/gemini", model="gemini-3-flash-preview")
        )
        resolved = env.apply_to_selection(base)
        self.assertEqual("google/gemini", resolved.provider)
        self.assertEqual("gemini-3-flash-preview", resolved.model)

    def test_apply_to_selection_keeps_base_when_llm_none(self) -> None:
        base = ModelSelection(provider="anthropic", model="claude-opus-4-7")
        env = Environ()
        resolved = env.apply_to_selection(base)
        self.assertEqual(base, resolved)

    def test_merge_provider_options_environ_wins(self) -> None:
        env = Environ(provider_options={"thinking": "xhigh", "timeout_seconds": 30})
        merged = env.merge_provider_options({"thinking": "low", "max_retries": 2})
        self.assertEqual(
            {"thinking": "xhigh", "max_retries": 2, "timeout_seconds": 30},
            merged,
        )

    def test_resolve_model_config_environ_overrides_selection(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = AppConfig.load(_write_multi_model_config(root))
            env = Environ(
                llm=ModelSelection(
                    provider="google/gemini", model="gemini-3-flash-preview"
                )
            )

            model = config.resolve_model_config(environ=env)

            self.assertEqual("google/gemini", model.provider)
            self.assertEqual("gemini-3-flash-preview", model.model)
            self.assertEqual(0.2, model.temperature)

    def test_resolve_model_config_environ_overrides_agent_selection(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = AppConfig.load(_write_multi_model_config(root))
            agent_selection = ModelSelection(
                provider="anthropic", model="claude-opus-4-7"
            )
            env = Environ(
                llm=ModelSelection(provider="anthropic", model="claude-sonnet-4-6")
            )

            model = config.resolve_model_config(agent_selection, environ=env)

            self.assertEqual("anthropic", model.provider)
            self.assertEqual("claude-sonnet-4-6", model.model)
            self.assertEqual(2048, model.max_output_tokens)

    def test_resolve_model_config_merges_environ_provider_options(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = AppConfig.load(_write_multi_model_config(root))
            env = Environ(provider_options={"thinking": "xhigh"})

            model = config.resolve_model_config(environ=env)

            self.assertEqual("anthropic", model.provider)
            self.assertEqual("claude-opus-4-7", model.model)
            self.assertEqual("xhigh", model.provider_options["thinking"])

    def test_run_open_creates_empty_environ(self) -> None:
        agent = Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_config=ModelConfig(
                provider="google/gemini",
                model="gemini-3-flash-preview",
                api_key="k",
            ),
            tool_ids=[],
        )
        provider = MagicMock()

        run = Run.open(agent=agent, provider=provider, tools=[])

        self.assertIsInstance(run.environ, Environ)
        self.assertIsNone(run.environ.llm)
        self.assertEqual({}, run.environ.provider_options)

    def test_apply_environ_model_updates_agent_and_provider(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = AppConfig.load(_write_multi_model_config(root))
            agent = Agent(
                agent_id="Pickle",
                workspace_path=root / "workspace",
                behavior_path=root / "agents" / "Pickle",
                behavior_instruction="You are Pickle.",
                model_config=config.resolve_model_config(),
                tool_ids=[],
            )
            stub_provider = MagicMock(name="stub-provider")
            new_provider = MagicMock(name="new-provider")

            with patch(
                "myopenclaw.runs.run.create_llm_provider",
                return_value=new_provider,
            ) as create_provider:
                run = Run.open(agent=agent, provider=stub_provider, tools=[])
                run.environ.llm = ModelSelection(
                    provider="anthropic", model="claude-sonnet-4-6"
                )
                run.environ.provider_options = {"thinking": "xhigh"}

                run.apply_environ_model(config)

            self.assertEqual("claude-sonnet-4-6", run.agent.model_config.model)
            self.assertEqual("xhigh", run.agent.model_config.provider_options["thinking"])
            self.assertIs(new_provider, run.provider)
            create_provider.assert_called_once_with(run.agent.model_config)


if __name__ == "__main__":
    unittest.main()
