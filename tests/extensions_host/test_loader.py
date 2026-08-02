"""发现与装载：用户级目录、失败隔离、同名覆盖、工具回滚。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from pickel.extensions_host.loader import load_extensions
from pickel.tools.bus import ToolBus, ToolSource


def _app_config(extensions: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(extensions=extensions or {})


def _write_extension(home: Path, name: str, body: str) -> None:
    ext_dir = home / "extensions" / name
    ext_dir.mkdir(parents=True)
    (ext_dir / "__init__.py").write_text(body, encoding="utf-8")


_RECALL_EXT = """
def setup(host):
    host.add_recall_source(lambda scope: f"recall-from-{host.name}")
"""

_TOOL_EXT = """
from pickel.tools.base import BaseTool, ToolSpec


class _Probe(BaseTool):
    spec = ToolSpec(
        name="probe",
        description="probe",
        input_schema={"type": "object", "properties": {}},
    )


def setup(host):
    host.register_tool(_Probe())
"""

_BROKEN_IMPORT_EXT = "raise RuntimeError('import time boom')\n"

_NO_SETUP_EXT = "VALUE = 1\n"

_SETUP_BOOM_AFTER_TOOL_EXT = """
from pickel.tools.base import BaseTool, ToolSpec


class _Probe(BaseTool):
    spec = ToolSpec(
        name="probe",
        description="probe",
        input_schema={"type": "object", "properties": {}},
    )


def setup(host):
    host.register_tool(_Probe())
    raise RuntimeError('setup boom')
"""

_ASYNC_EXT = """
async def setup(host):
    host.add_session_sync(lambda scope: f"sync-from-{host.name}")
"""


class LoaderDiscoveryTests(unittest.TestCase):
    def test_only_loads_explicitly_enabled_extensions(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_extension(home, "enabled", _RECALL_EXT)
            _write_extension(home, "disabled", _RECALL_EXT)

            result = load_extensions(
                tool_bus=ToolBus(),
                app_config=_app_config(),
                home=home,
                builtin_package=None,
                enabled_names={"enabled"},
            )

            self.assertEqual([], result.errors)
            self.assertEqual(["enabled"], result.registry.extension_names)

    def test_empty_enabled_extensions_starts_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_extension(home, "disabled", _RECALL_EXT)

            result = load_extensions(
                tool_bus=ToolBus(),
                app_config=_app_config(),
                home=home,
                builtin_package=None,
                enabled_names=set(),
            )

            self.assertEqual([], result.errors)
            self.assertEqual([], result.registry.extension_names)

    def test_loads_user_level_extension_and_collects_contribution(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_extension(home, "demo", _RECALL_EXT)

            result = load_extensions(
                tool_bus=ToolBus(),
                app_config=_app_config(),
                home=home,
                builtin_package=None,
            )

            self.assertEqual([], result.errors)
            self.assertEqual(["demo"], result.registry.extension_names)
            scope = SimpleNamespace(agent_id="Pickle", app_config=None)
            self.assertEqual(
                ["recall-from-demo"], result.registry.recall_sources(scope)
            )

    def test_registers_tools_under_extension_origin(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_extension(home, "probe_ext", _TOOL_EXT)
            bus = ToolBus()

            result = load_extensions(
                tool_bus=bus, app_config=_app_config(), home=home, builtin_package=None
            )

            self.assertEqual([], result.errors)
            entry = bus.get("ext__probe_ext__probe")
            self.assertEqual(ToolSource.EXTENSION, entry.source)

    def test_awaits_async_setup(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_extension(home, "async_ext", _ASYNC_EXT)

            result = load_extensions(
                tool_bus=ToolBus(),
                app_config=_app_config(),
                home=home,
                builtin_package=None,
            )

            self.assertEqual([], result.errors)
            scope = SimpleNamespace(agent_id="Pickle", app_config=None)
            self.assertEqual(
                ["sync-from-async_ext"], result.registry.session_syncs(scope)
            )

    def test_skips_underscore_and_dot_prefixed_entries(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_extension(home, "_private", _RECALL_EXT)
            _write_extension(home, ".hidden", _RECALL_EXT)
            _write_extension(home, "real", _RECALL_EXT)

            result = load_extensions(
                tool_bus=ToolBus(),
                app_config=_app_config(),
                home=home,
                builtin_package=None,
            )

            self.assertEqual(["real"], result.registry.extension_names)

    def test_missing_extensions_dir_is_not_an_error(self) -> None:
        with TemporaryDirectory() as tmp:
            result = load_extensions(
                tool_bus=ToolBus(),
                app_config=_app_config(),
                home=Path(tmp),
                builtin_package=None,
            )

            self.assertEqual([], result.errors)


class LoaderIsolationTests(unittest.TestCase):
    def test_import_failure_is_isolated_and_others_still_load(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_extension(home, "aaa_broken", _BROKEN_IMPORT_EXT)
            _write_extension(home, "zzz_healthy", _RECALL_EXT)

            result = load_extensions(
                tool_bus=ToolBus(),
                app_config=_app_config(),
                home=home,
                builtin_package=None,
            )

            self.assertEqual(1, len(result.errors))
            self.assertIn("aaa_broken", str(result.errors[0]))
            self.assertEqual(["zzz_healthy"], result.registry.extension_names)

    def test_module_without_setup_reports_a_clear_error(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_extension(home, "no_setup", _NO_SETUP_EXT)

            result = load_extensions(
                tool_bus=ToolBus(),
                app_config=_app_config(),
                home=home,
                builtin_package=None,
            )

            self.assertEqual(1, len(result.errors))
            self.assertIn("setup", str(result.errors[0]))

    def test_setup_failure_rolls_back_already_registered_tools(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_extension(home, "half_dead", _SETUP_BOOM_AFTER_TOOL_EXT)
            bus = ToolBus()

            result = load_extensions(
                tool_bus=bus, app_config=_app_config(), home=home, builtin_package=None
            )

            self.assertEqual(1, len(result.errors))
            # 半装状态必须回滚，否则 bus 里留着一个无人维护的工具
            self.assertEqual([], bus.list_names(source=ToolSource.EXTENSION))

    def test_invalid_config_section_is_a_load_error(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_extension(
                home,
                "cfg_ext",
                "from pydantic import BaseModel\n"
                "class _C(BaseModel):\n"
                "    count: int = 0\n"
                "def setup(host):\n"
                "    host.config(_C)\n",
            )

            result = load_extensions(
                tool_bus=ToolBus(),
                app_config=_app_config({"cfg_ext": {"count": "not-an-int"}}),
                home=home,
                builtin_package=None,
            )

            self.assertEqual(1, len(result.errors))


if __name__ == "__main__":
    unittest.main()
