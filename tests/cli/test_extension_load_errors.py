"""装载失败不阻止启动。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from pickel.extensions_host.loader import load_extensions
from pickel.tools.bus import ToolBus


class ExtensionLoadErrorTests(unittest.TestCase):
    def test_broken_extension_yields_error_without_raising(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            ext_dir = home / "extensions" / "broken"
            ext_dir.mkdir(parents=True)
            (ext_dir / "__init__.py").write_text(
                "raise RuntimeError('boom')\n", encoding="utf-8"
            )

            result = load_extensions(
                tool_bus=ToolBus(),
                app_config=SimpleNamespace(extensions={}),
                home=home,
                builtin_package=None,
            )

            self.assertEqual(1, len(result.errors))
            self.assertEqual([], result.registry.extension_names)


if __name__ == "__main__":
    unittest.main()
