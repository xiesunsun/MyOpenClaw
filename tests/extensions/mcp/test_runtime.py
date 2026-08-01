from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import mcp.types

from pickel.extensions.mcp.proxy import McpProxyTool
from pickel.extensions.mcp.runtime import McpServerRuntime
from pickel.extensions.mcp.connection import McpConnectionError
from pickel.extensions_host.host import ExtensionHost
from pickel.extensions_host.registry import ExtensionRegistry
from pickel.tools.base import ToolExecutionContext
from pickel.tools.bus import ToolBus, ToolSource
from pickel.tools.services import ToolServices
from pickel.runs.host_call_types import (
    STRUCTURED_INPUT_CALL,
    StructuredInputAnswer,
)
from pickel.runs.host_calls import HostCallContext, HostCallRouter

from tests.extensions.mcp.test_connection import fixture_spec


def _host(bus: ToolBus) -> ExtensionHost:
    return ExtensionHost(
        name="mcp",
        config_section=None,
        tool_bus=bus,
        registry=ExtensionRegistry(),
        app_config=None,
    )


def _context(*, host_calls=None) -> ToolExecutionContext:
    return ToolExecutionContext(
        agent_id="Pickle",
        session_id="s",
        workspace_path=Path("/tmp"),
        services=ToolServices(host_calls=host_calls),
        turn_id="turn-1",
        step_index=1,
        tool_call_id="tool-call-1",
    )


class McpServerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_registers_proxy_tools_on_bus(self) -> None:
        bus = ToolBus()
        runtime = McpServerRuntime(spec=fixture_spec(), host=_host(bus))
        try:
            await runtime.start()
            names = set(bus.list_names(source=ToolSource.MCP))
            self.assertEqual(
                {
                    "mcp__fixture__echo",
                    "mcp__fixture__boom",
                    "mcp__fixture__die",
                    "mcp__fixture__elicited",
                    "mcp__fixture__elicited_multi_round",
                },
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

    async def test_proxy_preserves_image_and_structured_content(self) -> None:
        async def fake_call(tool_name, arguments):
            return mcp.types.CallToolResult(
                content=[
                    mcp.types.TextContent(type="text", text="hello"),
                    mcp.types.ImageContent(
                        type="image", data="aGk=", mime_type="image/png"
                    ),
                    mcp.types.AudioContent(
                        type="audio", data="aGk=", mime_type="audio/wav"
                    ),
                ],
                structured_content={"answer": 42},
                is_error=False,
            )

        runtime = SimpleNamespace(call=fake_call, spec=SimpleNamespace(name="fake"))
        tool = mcp.types.Tool(
            name="t",
            description="d",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        )

        proxy = McpProxyTool(runtime, tool)
        result = await proxy.execute({}, _context())

        self.assertEqual("hello", result.content)
        self.assertEqual({"answer": 42}, result.structured_content)
        self.assertEqual({"type": "object"}, proxy.spec.output_schema)
        self.assertEqual(2, len(result.content_blocks))
        self.assertEqual("image", result.content_blocks[1].type)
        self.assertEqual("image/png", result.content_blocks[1].media_type)
        self.assertEqual(["audio"], result.metadata["unsupported_content"])
        self.assertEqual("audio", result.metadata["unsupported_mcp_content"][0]["type"])

    async def test_connection_loss_reconnects_without_replaying_tool(self) -> None:
        bus = ToolBus()
        runtime = McpServerRuntime(spec=fixture_spec(), host=_host(bus))
        connection = SimpleNamespace(
            is_alive=lambda: True,
            call_tool=AsyncMock(side_effect=McpConnectionError("lost")),
        )
        runtime._connection = connection
        runtime._reconnect = AsyncMock()

        with self.assertRaisesRegex(McpConnectionError, "was not retried"):
            await runtime.call("side_effect", {"value": 1})

        connection.call_tool.assert_awaited_once_with("side_effect", {"value": 1})
        runtime._reconnect.assert_awaited_once_with()

    async def test_proxy_drives_mcp2_input_required_through_host_call(self) -> None:
        bus = ToolBus()
        runtime = McpServerRuntime(spec=fixture_spec(), host=_host(bus))
        host_calls = HostCallRouter()
        seen = []

        async def handle(request, context):
            seen.append((request, context))
            return StructuredInputAnswer(
                action="accept",
                content={"name": "Ada", "count": 2},
            )

        host_calls.register(STRUCTURED_INPUT_CALL, handle)
        try:
            await runtime.start()
            tool = bus.get("mcp__fixture__elicited").tool
            result = await tool.execute({}, _context(host_calls=host_calls.client))
        finally:
            await runtime.close()

        self.assertFalse(result.is_error)
        self.assertEqual("elicited:Ada:2", result.content)
        self.assertEqual(1, len(seen))
        self.assertEqual("Provide user details", seen[0][0].message)
        self.assertEqual("turn-1", seen[0][1].turn_id)
        self.assertEqual("tool-call-1", seen[0][1].tool_call_id)

    async def test_proxy_drives_multiple_mcp2_input_required_rounds(self) -> None:
        bus = ToolBus()
        runtime = McpServerRuntime(spec=fixture_spec(), host=_host(bus))
        host_calls = HostCallRouter()
        messages = []

        async def handle(request, _context):
            messages.append(request.message)
            if "user details" in request.message:
                content = {"name": "Ada", "count": 2}
            else:
                content = {"project": "Pickel"}
            return StructuredInputAnswer(action="accept", content=content)

        host_calls.register(STRUCTURED_INPUT_CALL, handle)
        try:
            await runtime.start()
            tool = bus.get("mcp__fixture__elicited_multi_round").tool
            result = await tool.execute({}, _context(host_calls=host_calls.client))
        finally:
            await runtime.close()

        self.assertFalse(result.is_error)
        self.assertEqual("project:Pickel", result.content)
        self.assertEqual(
            ["Provide user details", "Provide a project for Ada"],
            messages,
        )

    async def test_resolves_multiple_embedded_input_requests(self) -> None:
        runtime = McpServerRuntime(spec=fixture_spec(), host=_host(ToolBus()))
        host_calls = HostCallRouter()

        async def handle(request, _context):
            field = next(iter(request.schema["properties"]))
            return StructuredInputAnswer(
                action="accept",
                content={field: field.upper()},
            )

        host_calls.register(STRUCTURED_INPUT_CALL, handle)
        requests = {
            "first": mcp.types.ElicitRequest(
                params=mcp.types.ElicitRequestFormParams(
                    message="First",
                    requested_schema={
                        "type": "object",
                        "properties": {"one": {"type": "string"}},
                    },
                )
            ),
            "second": mcp.types.ElicitRequest(
                params=mcp.types.ElicitRequestFormParams(
                    message="Second",
                    requested_schema={
                        "type": "object",
                        "properties": {"two": {"type": "string"}},
                    },
                )
            ),
        }

        responses = await runtime._resolve_input_requests(
            input_requests=requests,
            host_calls=host_calls.client,
            call_context=HostCallContext(
                session_id="s",
                turn_id="t",
                tool_call_id="tool-call",
            ),
            tool_name="tool",
        )

        self.assertEqual({"one": "ONE"}, responses["first"].content)
        self.assertEqual({"two": "TWO"}, responses["second"].content)

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
            # 让重连必然失败：偷换 spec 为坏命令
            runtime.spec = type(spec)(name=spec.name, command="/no/such/command-xyz")
            await die.execute({}, _context())

            result = await echo.execute({"text": "x"}, _context())

            self.assertTrue(result.is_error)
            self.assertEqual([], bus.list_names(source=ToolSource.MCP))
        finally:
            await runtime.close()
