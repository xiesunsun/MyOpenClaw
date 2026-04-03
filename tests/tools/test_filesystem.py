from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from myopenclaw.infrastructure.tools.base import ToolExecutionContext
from myopenclaw.infrastructure.tools.file_formatter import FileToolFormatter
from myopenclaw.infrastructure.tools.file_service import WorkspaceFileService
from myopenclaw.infrastructure.tools.file_tools import ReadFileTool, ReplaceTool, WriteFileTool
from myopenclaw.infrastructure.tools.policy import FullAccessPathPolicy, WorkspacePathAccessPolicy


class FilesystemToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_tool_supports_line_ranges(self) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "note.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
            tool = ReadFileTool(FileToolFormatter())

            result = await tool.execute(
                {
                    "path": "note.txt",
                    "start_line": 2,
                    "end_line": 3,
                },
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

        self.assertEqual("File: note.txt\n2: beta\n3: gamma", result.content)
        self.assertEqual(
            {
                "path": "note.txt",
                "start_line": 2,
                "end_line": 3,
                "truncated": False,
            },
            result.metadata,
        )

    async def test_read_tool_rejects_paths_outside_workspace(self) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            tool = ReadFileTool(FileToolFormatter())

            result = await tool.execute(
                {"path": "../secret.txt"},
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

        self.assertTrue(result.is_error)
        self.assertIn("outside the workspace", result.content)

    async def test_write_tool_can_create_and_replace_text(self) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            formatter = FileToolFormatter()
            write_tool = WriteFileTool(formatter)
            replace_tool = ReplaceTool(formatter)
            workspace_files = WorkspaceFileService(
                workspace_root=workspace,
                access_policy=WorkspacePathAccessPolicy(),
            )

            create_result = await write_tool.execute(
                {
                    "path": "note.txt",
                    "content": "hello world",
                    "if_exists": "error",
                },
                ToolExecutionContext(
                    agent_id="Pickle",
                    session_id="session-1",
                    workspace_path=workspace,
                    workspace_files=workspace_files,
                    shell_session_manager=None,
                ),
            )
            replace_result = await replace_tool.execute(
                {
                    "path": "note.txt",
                    "old_text": "world",
                    "new_text": "pickle",
                },
                ToolExecutionContext(
                    agent_id="Pickle",
                    session_id="session-1",
                    workspace_path=workspace,
                    workspace_files=workspace_files,
                    shell_session_manager=None,
                ),
            )

            content = (workspace / "note.txt").read_text(encoding="utf-8")

        self.assertFalse(create_result.is_error)
        self.assertFalse(replace_result.is_error)
        self.assertEqual("hello pickle", content)
        self.assertEqual(1, replace_result.metadata["match_count"])

    async def test_replace_tool_rejects_multiple_exact_matches(self) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "note.txt").write_text("dup\ndup\n", encoding="utf-8")
            tool = ReplaceTool(FileToolFormatter())

            result = await tool.execute(
                {
                    "path": "note.txt",
                    "old_text": "dup",
                    "new_text": "value",
                },
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

        self.assertTrue(result.is_error)
        self.assertIn("Found 2 exact matches", result.content)

    async def test_full_access_policy_allows_absolute_paths_outside_workspace(self) -> None:
        with TemporaryDirectory() as workspace_tmpdir, TemporaryDirectory() as external_tmpdir:
            workspace = Path(workspace_tmpdir)
            external_file = Path(external_tmpdir) / "secret.txt"
            external_file.write_text("outside\n", encoding="utf-8")
            tool = ReadFileTool(FileToolFormatter())

            result = await tool.execute(
                {"path": str(external_file)},
                ToolExecutionContext(
                    agent_id="Pickle",
                    session_id="session-1",
                    workspace_path=workspace,
                    workspace_files=WorkspaceFileService(
                        workspace_root=workspace,
                        access_policy=FullAccessPathPolicy(),
                    ),
                    shell_session_manager=None,
                ),
            )

        self.assertFalse(result.is_error)
        self.assertIn("outside", result.content)


if __name__ == "__main__":
    unittest.main()
