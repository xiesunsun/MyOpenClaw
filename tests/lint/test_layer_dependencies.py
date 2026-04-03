from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import textwrap
import unittest


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "check_layer_dependencies.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("check_layer_dependencies", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LayerDependencyTests(unittest.TestCase):
    def test_current_source_tree_has_no_layer_violations(self) -> None:
        module = load_module()

        violations = module.find_violations()

        self.assertEqual([], violations)

    def test_detects_disallowed_dependency(self) -> None:
        module = load_module()

        with TemporaryDirectory() as tmpdir:
            source_root = Path(tmpdir) / "src" / "myopenclaw"
            (source_root / "shared").mkdir(parents=True)
            (source_root / "shared" / "__init__.py").write_text("")
            (source_root / "cli").mkdir()
            (source_root / "cli" / "__init__.py").write_text("")
            (source_root / "shared" / "bad.py").write_text(
                textwrap.dedent(
                    """
                    from myopenclaw.cli.chat import ChatLoop
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            violations = module.find_violations(source_root=source_root)

        self.assertEqual(1, len(violations))
        self.assertEqual("shared", violations[0].source_package)
        self.assertEqual("cli", violations[0].target_package)


if __name__ == "__main__":
    unittest.main()
