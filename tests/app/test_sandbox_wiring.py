from pathlib import Path
from tempfile import TemporaryDirectory
import textwrap
import unittest
import unittest.mock

from pickel.app.boot import Boot
from pickel.config.app_config import AppConfig
from pickel.tools.sandbox import SandboxSettings
from tests.helpers.yaml_app_config import app_config_from_yaml_file

_CONFIG_YAML = """
default_agent: Pickle
default_file_access_mode: full
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
        self.assertFalse(settings.allow_disable)

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

    def test_build_run_passes_policy_to_shell_manager(self) -> None:
        # Run.open 会构造 provider（需要 key），只验证注入的 manager 带上了 policy
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            boot = self._boot(root)
            captured: dict[str, object] = {}

            def _capture(**kwargs):
                captured.update(kwargs)
                raise RuntimeError("stop after capture")

            with unittest.mock.patch("pickel.app.boot.Run.open", side_effect=_capture):
                with self.assertRaises(RuntimeError):
                    boot.build_run()

            manager = captured["shell_session_manager"]
            self.assertIsNotNone(manager.sandbox)
            self.assertTrue(manager.sandbox.strict)
