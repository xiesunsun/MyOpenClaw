"""setup 只在 enabled 时注册贡献。"""

import unittest
from types import SimpleNamespace

from pickel.extensions.openviking import setup
from pickel.extensions_host.host import ExtensionHost
from pickel.extensions_host.registry import ExtensionRegistry
from pickel.tools.bus import ToolBus


def _host(section: dict | None) -> tuple[ExtensionHost, ExtensionRegistry]:
    registry = ExtensionRegistry()
    host = ExtensionHost(
        name="openviking",
        config_section=section,
        tool_bus=ToolBus(),
        registry=registry,
    )
    return host, registry


_MINIMAL = {
    "base_url": "https://ov.example",
    "account_id": "acct",
    "user_id": "user",
    "user_key": "key",
}


class OpenVikingSetupTests(unittest.TestCase):
    def test_registers_nothing_when_section_absent(self) -> None:
        host, registry = _host(None)

        setup(host)

        self.assertEqual([], registry.recall_factories)
        self.assertEqual([], registry.sync_factories)

    def test_registers_nothing_when_disabled(self) -> None:
        host, registry = _host({**_MINIMAL, "enabled": False})

        setup(host)

        self.assertEqual([], registry.recall_factories)
        self.assertEqual([], registry.sync_factories)

    def test_registers_sync_when_enabled(self) -> None:
        host, registry = _host({**_MINIMAL, "enabled": True})

        setup(host)

        self.assertEqual(1, len(registry.sync_factories))

    def test_recall_factory_registered_only_when_session_recall_enabled(self) -> None:
        host_off, registry_off = _host(
            {**_MINIMAL, "enabled": True, "session_recall": {"enabled": False}}
        )
        setup(host_off)

        host_on, registry_on = _host(
            {**_MINIMAL, "enabled": True, "session_recall": {"enabled": True}}
        )
        setup(host_on)

        self.assertEqual([], registry_off.recall_factories)
        self.assertEqual(1, len(registry_on.recall_factories))

    def test_factory_returns_none_for_agent_without_remote_id(self) -> None:
        host, registry = _host({**_MINIMAL, "enabled": True})
        setup(host)

        app_config = SimpleNamespace(
            default_agent="Pickle",
            get_agent_config=lambda agent_id=None: SimpleNamespace(
                remote_agent_id=None
            ),
        )
        scope = SimpleNamespace(agent_id="Pickle", app_config=app_config)

        self.assertIsNone(registry.sync_factories[0](scope))


if __name__ == "__main__":
    unittest.main()
