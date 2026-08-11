import textwrap
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from pickel.app.boot import Boot
from pickel.config.app_config import AppConfig
from pickel.tools.sandbox import SandboxSettings
from tests.helpers.yaml_app_config import app_config_from_yaml_file

_CONFIG_YAML = """
default_agent: Pickle
default_file_access_mode: full
default_llm:
  provider: anthropic
  model: claude-test
providers:
  anthropic:
    models:
      claude-test:
        api_key: test-key
        temperature: 1.0
        max_output_tokens: 1024
        provider_options: {}
sandbox:
  strict: true
agents:
  Pickle:
    workspace_path: workspace
    behavior_path: agents/Pickle
    tools:
      - bash
"""


class SandboxConfigTests(unittest.TestCase):
    def test_sandbox_settings_defaults(self) -> None:
        settings = SandboxSettings()

        self.assertTrue(settings.enabled)
        self.assertFalse(settings.strict)

    def test_app_config_has_sandbox_field(self) -> None:
        self.assertIn("sandbox", AppConfig.model_fields)


class SandboxWiringTests(unittest.TestCase):
    def _boot(self, root: Path) -> Boot:
        (root / "agents" / "Pickle").mkdir(parents=True)
        (root / "agents" / "Pickle" / "AGENT.md").write_text(
            "You are Pickle.\n", encoding="utf-8"
        )
        (root / "workspace").mkdir()
        config_path = root / "config.yaml"
        config_path.write_text(textwrap.dedent(_CONFIG_YAML).strip(), encoding="utf-8")
        return Boot(app_config_from_yaml_file(config_path))

    def test_boot_builds_policy_from_settings(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)

            policy = self._boot(root).sandbox_policy

            self.assertTrue(policy.enabled)
            self.assertTrue(policy.strict)
            self.assertEqual(root.resolve(), policy.project_root.resolve())

    def test_build_agent_runtime_passes_policy_to_local_bash(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            boot = self._boot(root)

            def _provider(config, *, artifact_service):
                return SimpleNamespace(artifact_service=artifact_service)

            with unittest.mock.patch(
                "pickel.app.boot.AnthropicProvider.from_config",
                side_effect=_provider,
            ):
                _, runtime = boot.build_agent_runtime()

            bash = runtime.bindings.tool_services.bash
            self.assertIsNotNone(bash.sandbox)
            self.assertTrue(bash.sandbox.strict)
