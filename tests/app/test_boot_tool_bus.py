"""Boot 透传进程级 bus；reload 后仍是同一实例。"""

import unittest
from types import SimpleNamespace

from pickel.tools.bus import ToolBus, ToolSource
from pickel.tools.catalog import install_builtin_tools


class InstallBuiltinToolsTests(unittest.TestCase):
    def test_install_registers_every_builtin_as_builtin_source(self) -> None:
        bus = ToolBus()

        install_builtin_tools(bus)

        names = bus.list_names(source=ToolSource.BUILTIN)
        self.assertIn("read", names)
        self.assertIn("bash", names)
        self.assertNotIn("shell_exec", names)
        self.assertNotIn("echo", names)
        self.assertNotIn("tool_set_active", names)
        self.assertEqual(sorted(names), sorted(bus.list_names()))

    def test_install_is_idempotent(self) -> None:
        bus = ToolBus()

        install_builtin_tools(bus)
        first = sorted(bus.list_names())
        install_builtin_tools(bus)

        self.assertEqual(first, sorted(bus.list_names()))


class BootToolBusTests(unittest.TestCase):
    def test_boot_creates_bus_with_builtins_when_not_injected(self) -> None:
        from pickel.app.boot import Boot

        boot = Boot.from_config(SimpleNamespace())

        self.assertIn("read", boot.tool_bus.list_names())

    def test_boot_reuses_injected_bus(self) -> None:
        from pickel.app.boot import Boot

        bus = ToolBus()
        install_builtin_tools(bus)

        boot = Boot.from_config(SimpleNamespace(), tool_bus=bus)

        self.assertIs(bus, boot.tool_bus)


if __name__ == "__main__":
    unittest.main()
