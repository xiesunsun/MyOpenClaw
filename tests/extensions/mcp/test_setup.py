import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock

import pickel.extensions.mcp as mcp_extension
from pickel.extensions_host.host import ExtensionHost
from pickel.extensions_host.registry import ExtensionRegistry
from pickel.tools.bus import ToolBus, ToolSource

from tests.extensions.mcp.test_connection import FIXTURE


def _mcp_json(root: Path, servers: dict) -> None:
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8"
    )


def _host(bus: ToolBus, root: Path, section: dict | None = None) -> ExtensionHost:
    return ExtensionHost(
        name="mcp",
        config_section=section,
        tool_bus=bus,
        registry=ExtensionRegistry(),
        app_config=SimpleNamespace(root=root),
    )


def _fixture_entry() -> dict:
    return {"command": sys.executable, "args": [str(FIXTURE)]}


class McpSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_connects_and_scope_close_unregisters(self) -> None:
        bus = ToolBus()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mcp_json(root, {"fixture": _fixture_entry()})
            host = _host(bus, root)
            with mock.patch.object(
                mcp_extension, "home_dir", return_value=root / "nohome"
            ):
                await mcp_extension.setup(host)
            try:
                self.assertIn(
                    "mcp__fixture__echo", bus.list_names(source=ToolSource.MCP)
                )
            finally:
                await host.scope.close()
        self.assertEqual([], bus.list_names(source=ToolSource.MCP))

    async def test_failing_server_is_isolated(self) -> None:
        bus = ToolBus()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mcp_json(
                root,
                {
                    "broken": {"command": "/no/such/command-xyz"},
                    "fixture": _fixture_entry(),
                },
            )
            host = _host(bus, root)
            with mock.patch.object(
                mcp_extension, "home_dir", return_value=root / "nohome"
            ):
                await mcp_extension.setup(host)
            try:
                names = bus.list_names(source=ToolSource.MCP)
                self.assertIn("mcp__fixture__echo", names)
                self.assertEqual(
                    [], [n for n in names if n.startswith("mcp__broken__")]
                )
                statuses = host._registry.mcp_status_source.snapshot().servers
                self.assertEqual(
                    {"broken": "failed", "fixture": "connected"},
                    {item.name: item.status for item in statuses},
                )
            finally:
                await host.scope.close()

    async def test_disabled_via_settings_registers_nothing(self) -> None:
        bus = ToolBus()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mcp_json(root, {"fixture": _fixture_entry()})
            with mock.patch.object(
                mcp_extension, "home_dir", return_value=root / "nohome"
            ):
                await mcp_extension.setup(_host(bus, root, section={"enabled": False}))
            self.assertEqual([], bus.list_names(source=ToolSource.MCP))

    async def test_no_mcp_json_is_silent(self) -> None:
        bus = ToolBus()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            host = _host(bus, root)
            with mock.patch.object(
                mcp_extension, "home_dir", return_value=root / "nohome"
            ):
                await mcp_extension.setup(host)
            self.assertEqual([], bus.list_names(source=ToolSource.MCP))
            self.assertEqual((), host._registry.mcp_status_source.snapshot().servers)
            await host.scope.close()

    async def test_old_instance_close_does_not_remove_new_instance_tools(self) -> None:
        bus = ToolBus()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mcp_json(root, {"fixture": _fixture_entry()})
            old_host = _host(bus, root)
            new_host = _host(bus, root)
            with mock.patch.object(
                mcp_extension, "home_dir", return_value=root / "nohome"
            ):
                await mcp_extension.setup(old_host)
                await mcp_extension.setup(new_host)

            # 同名重载会替换 ToolBus Entry；旧实例的精确 lease 只能清理
            # 旧 Entry，不能误删新 Generation 的工具。
            await old_host.scope.close()
            self.assertIn("mcp__fixture__echo", bus.list_names(source=ToolSource.MCP))
            await new_host.scope.close()
        self.assertEqual([], bus.list_names(source=ToolSource.MCP))
