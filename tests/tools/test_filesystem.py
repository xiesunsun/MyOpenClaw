import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pickel.tools.base import ToolExecutionContext
from pickel.tools.file_formatter import FileToolFormatter
from pickel.tools.file_service import WorkspaceFileService
from pickel.tools.file_tools import EditTool, ReadTool, WriteTool
from pickel.tools.policy import FullAccessPathPolicy, WorkspacePathAccessPolicy
from pickel.tools.services import ToolServices


class FilesystemToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_tool_marks_character_truncation_in_model_content(self) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "note.txt").write_text("a" * 60_000, encoding="utf-8")
            tool = ReadTool(FileToolFormatter())

            result = await tool.execute(
                {"path": "note.txt"},
                ToolExecutionContext(
                    agent_id="Pickle",
                    session_id="session-1",
                    workspace_path=workspace,
                    services=ToolServices(
                        workspace_files=WorkspaceFileService(
                            workspace_root=workspace,
                            access_policy=WorkspacePathAccessPolicy(),
                        )
                    ),
                ),
            )

        self.assertEqual(50_000, len(result.content))
        self.assertIn("Output truncated", result.content)
        self.assertTrue(result.metadata["truncated"])

    async def test_read_tool_supports_line_ranges(self) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "note.txt").write_text(
                "alpha\nbeta\ngamma\n", encoding="utf-8"
            )
            tool = ReadTool(FileToolFormatter())

            result = await tool.execute(
                {
                    "path": "note.txt",
                    "offset": 2,
                    "limit": 2,
                },
                ToolExecutionContext(
                    agent_id="Pickle",
                    session_id="session-1",
                    workspace_path=workspace,
                    services=ToolServices(
                        workspace_files=WorkspaceFileService(
                            workspace_root=workspace,
                            access_policy=WorkspacePathAccessPolicy(),
                        )
                    ),
                ),
            )

        self.assertEqual("2: beta\n3: gamma", result.content)
        self.assertEqual(
            {
                "path": "note.txt",
                "offset": 2,
                "end_line": 3,
                "total_lines": 3,
                "truncated": False,
            },
            result.metadata,
        )

    async def test_read_tool_rejects_paths_outside_workspace(self) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            tool = ReadTool(FileToolFormatter())

            result = await tool.execute(
                {"path": "../secret.txt"},
                ToolExecutionContext(
                    agent_id="Pickle",
                    session_id="session-1",
                    workspace_path=workspace,
                    services=ToolServices(
                        workspace_files=WorkspaceFileService(
                            workspace_root=workspace,
                            access_policy=WorkspacePathAccessPolicy(),
                        )
                    ),
                ),
            )

        self.assertTrue(result.is_error)
        self.assertIn("outside the workspace", result.content)

    async def test_write_tool_can_create_and_replace_text(self) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            formatter = FileToolFormatter()
            write_tool = WriteTool(formatter)
            edit_tool = EditTool(formatter)
            workspace_files = WorkspaceFileService(
                workspace_root=workspace,
                access_policy=WorkspacePathAccessPolicy(),
            )

            create_result = await write_tool.execute(
                {
                    "path": "note.txt",
                    "content": "hello world",
                },
                ToolExecutionContext(
                    agent_id="Pickle",
                    session_id="session-1",
                    workspace_path=workspace,
                    services=ToolServices(workspace_files=workspace_files),
                ),
            )
            edit_result = await edit_tool.execute(
                {
                    "path": "note.txt",
                    "old_text": "world",
                    "new_text": "pickle",
                },
                ToolExecutionContext(
                    agent_id="Pickle",
                    session_id="session-1",
                    workspace_path=workspace,
                    services=ToolServices(workspace_files=workspace_files),
                ),
            )

            content = (workspace / "note.txt").read_text(encoding="utf-8")

        self.assertFalse(create_result.is_error)
        self.assertFalse(edit_result.is_error)
        self.assertEqual("hello pickle", content)
        self.assertEqual(1, edit_result.metadata["match_count"])
        self.assertIn("-hello world", edit_result.content)
        self.assertIn("+hello pickle", edit_result.content)

    async def test_replace_tool_rejects_multiple_exact_matches(self) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "note.txt").write_text("dup\ndup\n", encoding="utf-8")
            tool = EditTool(FileToolFormatter())

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
                    services=ToolServices(
                        workspace_files=WorkspaceFileService(
                            workspace_root=workspace,
                            access_policy=WorkspacePathAccessPolicy(),
                        )
                    ),
                ),
            )

        self.assertTrue(result.is_error)
        self.assertIn("Found 2 exact matches", result.content)

    async def test_full_access_policy_allows_absolute_paths_outside_workspace(
        self,
    ) -> None:
        with (
            TemporaryDirectory() as workspace_tmpdir,
            TemporaryDirectory() as external_tmpdir,
        ):
            workspace = Path(workspace_tmpdir)
            external_file = Path(external_tmpdir) / "secret.txt"
            external_file.write_text("outside\n", encoding="utf-8")
            tool = ReadTool(FileToolFormatter())

            result = await tool.execute(
                {"path": str(external_file)},
                ToolExecutionContext(
                    agent_id="Pickle",
                    session_id="session-1",
                    workspace_path=workspace,
                    services=ToolServices(
                        workspace_files=WorkspaceFileService(
                            workspace_root=workspace,
                            access_policy=FullAccessPathPolicy(),
                        )
                    ),
                ),
            )

        self.assertFalse(result.is_error)
        self.assertIn("outside", result.content)


if __name__ == "__main__":
    unittest.main()
