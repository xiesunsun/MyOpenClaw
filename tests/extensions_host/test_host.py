from pathlib import Path
import unittest
from types import SimpleNamespace

from pydantic import BaseModel

from pickel.extensions_host.errors import ExtensionConfigError
from pickel.extensions_host.host import ExtensionHost
from pickel.extensions_host.registry import AgentScope, ExtensionRegistry
from pickel.extensions_host.mcp_status import McpStatusSnapshot
from pickel.tools.base import BaseTool, ToolSpec
from pickel.tools.bus import ToolBus, ToolSource


class _DemoConfig(BaseModel):
    enabled: bool = False
    base_url: str = ""


def _stub_tool(name: str) -> BaseTool:
    class _Stub(BaseTool):
        spec = ToolSpec(
            name=name,
            description=f"{name} description",
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "object"},
        )

    return _Stub()


def _host(
    *, name: str = "demo", section: dict | None = None
) -> tuple[ExtensionHost, ToolBus, ExtensionRegistry]:
    bus = ToolBus()
    registry = ExtensionRegistry()
    host = ExtensionHost(
        name=name,
        config_section=section,
        tool_bus=bus,
        registry=registry,
    )
    return host, bus, registry


def _scope() -> AgentScope:
    return AgentScope(agent_id="Pickle", app_config=SimpleNamespace())


class ExtensionHostToolTests(unittest.TestCase):
    def test_register_tool_lands_in_bus_under_extension_origin(self) -> None:
        host, bus, _ = _host(name="openviking")

        host.register_tool(_stub_tool("recall_search"))

        entry = bus.get("ext__openviking__recall_search")
        self.assertEqual(ToolSource.EXTENSION, entry.source)
        self.assertEqual("openviking", entry.origin)


class ExtensionHostConfigTests(unittest.TestCase):
    def test_config_parses_own_section(self) -> None:
        host, _, _ = _host(section={"enabled": True, "base_url": "https://x"})

        config = host.config(_DemoConfig)

        self.assertTrue(config.enabled)
        self.assertEqual("https://x", config.base_url)

    def test_config_returns_none_when_section_absent(self) -> None:
        host, _, _ = _host(section=None)

        self.assertIsNone(host.config(_DemoConfig))

    def test_invalid_section_raises_extension_config_error(self) -> None:
        host, _, _ = _host(section={"enabled": "not-a-bool-at-all"})

        with self.assertRaises(ExtensionConfigError):
            host.config(_DemoConfig)


class ExtensionRegistryTests(unittest.TestCase):
    def test_factories_are_evaluated_with_scope_and_none_filtered(self) -> None:
        host, _, registry = _host()
        host.add_recall_source(lambda scope: f"recall-{scope.agent_id}")
        host.add_recall_source(lambda scope: None)

        sources = registry.recall_sources(_scope())

        self.assertEqual(["recall-Pickle"], sources)

    def test_failing_factory_is_skipped_without_breaking_others(self) -> None:
        host, _, registry = _host()

        def _boom(scope: AgentScope):
            raise RuntimeError("factory exploded")

        host.add_hook_handler(_boom)
        host.add_hook_handler(lambda scope: "healthy-handler")

        handlers = registry.hook_handlers(_scope())

        self.assertEqual(["healthy-handler"], handlers)

    def test_registry_records_extension_names(self) -> None:
        bus = ToolBus()
        registry = ExtensionRegistry()
        for name in ("alpha", "beta"):
            ExtensionHost(
                name=name,
                config_section=None,
                tool_bus=bus,
                registry=registry,
            ).add_recall_source(lambda scope: None)

        self.assertEqual(["alpha", "beta"], registry.extension_names)


if __name__ == "__main__":
    unittest.main()


class McpHostApiTests(unittest.TestCase):
    def _mcp_host(self, bus: ToolBus) -> ExtensionHost:
        return ExtensionHost(
            name="mcp",
            config_section=None,
            tool_bus=bus,
            registry=ExtensionRegistry(),
            app_config=SimpleNamespace(root=Path("/tmp/project")),
        )

    def test_register_mcp_tool_uses_mcp_prefix_and_server_origin(self) -> None:
        bus = ToolBus()
        host = self._mcp_host(bus)

        qualified = host.register_mcp_tool(_stub_tool("create_issue"), server="github")

        self.assertEqual("mcp__github__create_issue", qualified)
        self.assertEqual("github", bus.get(qualified).origin)
        self.assertIs(ToolSource.MCP, bus.get(qualified).source)

    def test_app_config_is_exposed(self) -> None:
        host = self._mcp_host(ToolBus())

        self.assertEqual(Path("/tmp/project"), host.app_config.root)

    def test_register_mcp_status_source_is_typed_and_unique(self) -> None:
        bus = ToolBus()
        registry = ExtensionRegistry()
        host = ExtensionHost(
            name="mcp",
            config_section=None,
            tool_bus=bus,
            registry=registry,
        )
        source = SimpleNamespace(snapshot=lambda: McpStatusSnapshot())

        host.register_mcp_status_source(source)

        self.assertIs(source, registry.mcp_status_source)
        with self.assertRaisesRegex(ValueError, "already registered"):
            host.register_mcp_status_source(source)
