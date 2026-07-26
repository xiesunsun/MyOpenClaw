from pathlib import Path
from types import SimpleNamespace
import unittest

import mcp.types

from pickel.extensions.mcp.proxy import McpProxyTool
from pickel.extensions.mcp.runtime import McpServerRuntime
from pickel.extensions_host.host import ExtensionHost
from pickel.extensions_host.registry import ExtensionRegistry
from pickel.tools.base import ToolExecutionContext
from pickel.tools.bus import ToolBus, ToolSource
from pickel.tools.services import ToolServices

from tests.extensions.mcp.test_connection import fixture_spec


def _host(bus: ToolBus) -> ExtensionHost:
    return ExtensionHost(
        name="mcp",
        config_section=None,
        tool_bus=bus,
        registry=ExtensionRegistry(),
        app_config=None,
    )


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        agent_id="Pickle",
        session_id="s",
        workspace_path=Path("/tmp"),
        services=ToolServices(),
    )


class McpServerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_registers_proxy_tools_on_bus(self) -> None:
        bus = ToolBus()
        runtime = McpServerRuntime(spec=fixture_spec(), host=_host(bus))
        try:
            await runtime.start()
            names = set(bus.list_names(source=ToolSource.MCP))
            self.assertEqual(
                {"mcp__fixture__echo", "mcp__fixture__boom", "mcp__fixture__die"},
                names,
            )
        finally:
            await runtime.close()
        self.assertEqual([], bus.list_names(source=ToolSource.MCP))

    async def test_proxy_execute_converts_text_and_error(self) -> None:
        bus = ToolBus()
        runtime = McpServerRuntime(spec=fixture_spec(), host=_host(bus))
        try:
            await runtime.start()
            echo = bus.get("mcp__fixture__echo").tool
            result = await echo.execute({"text": "hi"}, _context())
            self.assertFalse(result.is_error)
            self.assertEqual("echo:hi", result.content)
            self.assertEqual("fixture", result.metadata["server"])

            boom = bus.get("mcp__fixture__boom").tool
            result = await boom.execute({}, _context())
            self.assertTrue(result.is_error)
        finally:
            await runtime.close()

    async def test_proxy_marks_non_text_content_as_unsupported(self) -> None:
        async def fake_call(tool_name, arguments):
            return mcp.types.CallToolResult(
                content=[
                    mcp.types.TextContent(type="text", text="hello"),
                    mcp.types.ImageContent(
                        type="image", data="aGk=", mimeType="image/png"
                    ),
                ],
                isError=False,
            )

        runtime = SimpleNamespace(call=fake_call, spec=SimpleNamespace(name="fake"))
        tool = mcp.types.Tool(name="t", description="d", inputSchema={"type": "object"})

        result = await McpProxyTool(runtime, tool).execute({}, _context())

        self.assertEqual("hello\n[unsupported content: image]", result.content)
        self.assertEqual(["image"], result.metadata["unsupported_content"])

    async def test_call_reconnects_after_server_death(self) -> None:
        bus = ToolBus()
        runtime = McpServerRuntime(spec=fixture_spec(), host=_host(bus))
        try:
            await runtime.start()
            die = bus.get("mcp__fixture__die").tool
            first = await die.execute({}, _context())
            self.assertTrue(first.is_error)

            echo = bus.get("mcp__fixture__echo").tool
            second = await echo.execute({"text": "back"}, _context())
            self.assertFalse(second.is_error)
            self.assertEqual("echo:back", second.content)
        finally:
            await runtime.close()

    async def test_reconnect_failure_unregisters_server_tools(self) -> None:
        bus = ToolBus()
        spec = fixture_spec()
        runtime = McpServerRuntime(spec=spec, host=_host(bus))
        try:
            await runtime.start()
            die = bus.get("mcp__fixture__die").tool
            echo = bus.get("mcp__fixture__echo").tool
            await die.execute({}, _context())
            # 让重连必然失败：偷换 spec 为坏命令
            runtime.spec = type(spec)(name=spec.name, command="/no/such/command-xyz")

            result = await echo.execute({"text": "x"}, _context())

            self.assertTrue(result.is_error)
            self.assertEqual([], bus.list_names(source=ToolSource.MCP))
        finally:
            await runtime.close()
