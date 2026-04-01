from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from myopenclaw.tools.base import ToolExecutionContext
from myopenclaw.tools.catalog import builtin_tools
from myopenclaw.tools.policy import WorkspacePathAccessPolicy
from myopenclaw.tools.registry import ToolRegistry


class BuiltinToolTests(unittest.IsolatedAsyncioTestCase):
    def test_builtin_tool_catalog_can_seed_registry(self) -> None:
        registry = ToolRegistry(tools=builtin_tools())

        tools = registry.resolve_many(["echo", "read", "write", "shell_exec", "shell_restart", "shell_close"])

        self.assertEqual(
            ["echo", "read", "write", "shell_exec", "shell_restart", "shell_close"],
            [tool.spec.name for tool in tools],
        )

    async def test_builtin_read_tool_reads_relative_path_from_workspace(self) -> None:
        registry = ToolRegistry(tools=builtin_tools())
        read_tool = registry.resolve("read")

        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "note.txt").write_text("hello\nworld\n", encoding="utf-8")

            result = await read_tool.execute(
                {"path": "note.txt"},
                ToolExecutionContext(
                    agent_id="Pickle",
                    session_id="session-1",
                    workspace_path=workspace,
                    path_policy=WorkspacePathAccessPolicy(),
                    shell_session_manager=None,
                ),
            )

        self.assertIn("1: hello", result.content)
        self.assertIn("2: world", result.content)
        self.assertFalse(result.is_error)


if __name__ == "__main__":
    unittest.main()
