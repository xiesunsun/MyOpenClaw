from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap
import unittest

from myopenclaw.agents.agent import Agent
from myopenclaw.app.assembly import AppAssembly
from myopenclaw.config.app_config import AppConfig


class AppAssemblyTests(unittest.TestCase):
    def test_resolve_agent_loads_behavior_and_declared_defaults(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "agents" / "Pickle").mkdir(parents=True)
            (root / "agents" / "Pickle" / "AGENT.md").write_text("You are Pickle.\n")
            (root / ".agent" / "skills" / "excel").mkdir(parents=True)
            (root / ".agent" / "skills" / "excel" / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: excel
                    description: Analyze spreadsheets.
                    ---

                    # Excel
                    """
                ),
                encoding="utf-8",
            )
            (root / "workspace").mkdir()
            config_path = root / "config.yaml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    default_agent: Pickle
                    default_file_access_mode: full
                    default_skills_path: .agent/skills
                    default_llm:
                      provider: google/gemini
                      model: gemini-3-flash-preview
                    providers:
                      google/gemini:
                        models:
                          gemini-3-flash-preview:
                            temperature: 1.0
                            max_output_tokens: 1024
                            provider_options: {}
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                        tools:
                          - echo
                    """
                ).strip()
            )

            config = AppConfig.load(config_path)
            agent = AppAssembly(config).resolve_agent()

            self.assertIsInstance(agent, Agent)
            self.assertEqual("Pickle", agent.agent_id)
            self.assertEqual("You are Pickle.", agent.behavior_instruction)
            self.assertEqual(["echo"], agent.tool_ids)
            self.assertEqual(1, len(agent.skills))
            self.assertIn("Available skills:", agent.system_instruction)
            self.assertIn("excel: Analyze spreadsheets.", agent.system_instruction)

    def test_resolve_agent_rejects_skills_outside_workspace_without_full_access(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "agents" / "Pickle").mkdir(parents=True)
            (root / "agents" / "Pickle" / "AGENT.md").write_text("You are Pickle.\n")
            (root / ".agent" / "skills" / "excel").mkdir(parents=True)
            (root / ".agent" / "skills" / "excel" / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: excel
                    description: Analyze spreadsheets.
                    ---
                    """
                ),
                encoding="utf-8",
            )
            (root / "workspace").mkdir()
            config_path = root / "config.yaml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    default_agent: Pickle
                    default_file_access_mode: workspace
                    default_skills_path: .agent/skills
                    default_llm:
                      provider: google/gemini
                      model: gemini-3-flash-preview
                    providers:
                      google/gemini:
                        models:
                          gemini-3-flash-preview:
                            temperature: 1.0
                            max_output_tokens: 1024
                            provider_options: {}
                    agents:
                      Pickle:
                        workspace_path: workspace
                        behavior_path: agents/Pickle
                    """
                ).strip()
            )

            config = AppConfig.load(config_path)

            with self.assertRaisesRegex(ValueError, "requires file_access_mode: full"):
                AppAssembly(config).resolve_agent()


if __name__ == "__main__":
    unittest.main()
