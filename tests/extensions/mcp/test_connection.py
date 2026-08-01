from pathlib import Path
import sys
import unittest

from pickel.extensions.mcp.config import McpServerSpec
from pickel.extensions.mcp.connection import McpConnection, McpConnectionError

FIXTURE = Path(__file__).parent / "fixture_server.py"


def fixture_spec(name: str = "fixture") -> McpServerSpec:
    return McpServerSpec(name=name, command=sys.executable, args=(str(FIXTURE),))


class McpConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_discovers_tools_and_call_roundtrips(self) -> None:
        connection = McpConnection(fixture_spec())
        try:
            await connection.open()
            self.assertTrue(connection.is_alive())
            self.assertEqual(
                {"boom", "die", "echo", "elicited", "elicited_multi_round"},
                {tool.name for tool in connection.tools},
            )
            result = await connection.call_tool("echo", {"text": "hi"})
            self.assertFalse(bool(result.is_error))
            self.assertEqual("2026-07-28", connection.protocol_version)
            self.assertIsNotNone(connection.server_capabilities)
            self.assertEqual("echo:hi", result.content[0].text)
        finally:
            await connection.close()
        self.assertFalse(connection.is_alive())

    async def test_tool_error_is_result_not_exception(self) -> None:
        connection = McpConnection(fixture_spec())
        try:
            await connection.open()
            result = await connection.call_tool("boom", {})
            self.assertTrue(bool(result.is_error))
        finally:
            await connection.close()

    async def test_open_failure_raises(self) -> None:
        spec = McpServerSpec(name="broken", command="/no/such/command-xyz")
        connection = McpConnection(spec)
        with self.assertRaises(McpConnectionError):
            await connection.open()
        await connection.close()

    async def test_call_after_server_death_raises_connection_error(self) -> None:
        connection = McpConnection(fixture_spec())
        try:
            await connection.open()
            with self.assertRaises(McpConnectionError):
                await connection.call_tool("die", {})
            with self.assertRaises(McpConnectionError):
                await connection.call_tool("echo", {"text": "x"})
        finally:
            await connection.close()
