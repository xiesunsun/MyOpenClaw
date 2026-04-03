from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from myopenclaw.tools.base import ToolExecutionContext
from myopenclaw.tools.catalog import builtin_tools
from myopenclaw.tools.file_service import WorkspaceFileService
from myopenclaw.tools.policy import WorkspacePathAccessPolicy
from myopenclaw.tools.registry import ToolRegistry


class BuiltinToolTests(unittest.IsolatedAsyncioTestCase):
    def test_builtin_tool_catalog_can_seed_registry(self) -> None:
        registry = ToolRegistry(tools=builtin_tools())

        tools = registry.resolve_many(
            [
                "echo",
                "list_directory",
                "glob_search",
                "grep_search",
                "read_file",
                "read_many_files",
                "replace",
                "write_file",
                "shell_exec",
                "shell_restart",
                "shell_close",
            ]
        )

        self.assertEqual(
            [
                "echo",
                "list_directory",
                "glob_search",
                "grep_search",
                "read_file",
                "read_many_files",
                "replace",
                "write_file",
                "shell_exec",
                "shell_restart",
                "shell_close",
            ],
            [tool.spec.name for tool in tools],
        )

    async def test_builtin_read_tool_reads_relative_path_from_workspace(self) -> None:
        registry = ToolRegistry(tools=builtin_tools())
        read_tool = registry.resolve("read_file")

        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "note.txt").write_text("hello\nworld\n", encoding="utf-8")

            result = await read_tool.execute(
                {"path": "note.txt"},
                ToolExecutionContext(
                    agent_id="Pickle",
                    session_id="session-1",
                    workspace_path=workspace,
                    workspace_files=WorkspaceFileService(
                        workspace_root=workspace,
                        access_policy=WorkspacePathAccessPolicy(),
                    ),
                    shell_session_manager=None,
                ),
            )

        self.assertIn("File: note.txt", result.content)
        self.assertIn("1: hello", result.content)
        self.assertIn("2: world", result.content)
        self.assertFalse(result.is_error)


if __name__ == "__main__":
    unittest.main()
