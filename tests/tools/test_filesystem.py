from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from myopenclaw.tools.base import ToolExecutionContext
from myopenclaw.tools.filesystem import ReadTool, WriteTool
from myopenclaw.tools.policy import WorkspacePathAccessPolicy


class FilesystemToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_tool_supports_line_ranges(self) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "note.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
            tool = ReadTool()

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
                    path_policy=WorkspacePathAccessPolicy(),
                    shell_session_manager=None,
                ),
            )

        self.assertEqual("2: beta\n3: gamma", result.content)
        self.assertEqual(
            {
                "path": str((workspace / "note.txt").resolve()),
                "start_line": 2,
                "end_line": 3,
                "truncated": False,
            },
            result.metadata,
        )

    async def test_read_tool_rejects_paths_outside_workspace(self) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            tool = ReadTool()

            result = await tool.execute(
                {"path": "../secret.txt"},
                ToolExecutionContext(
                    agent_id="Pickle",
                    session_id="session-1",
                    workspace_path=workspace,
                    path_policy=WorkspacePathAccessPolicy(),
                    shell_session_manager=None,
                ),
            )

        self.assertTrue(result.is_error)
        self.assertIn("outside the workspace", result.content)

    async def test_write_tool_can_create_and_replace_text(self) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            tool = WriteTool()

            create_result = await tool.execute(
                {
                    "path": "note.txt",
                    "action": "create",
                    "content": "hello world",
                },
                ToolExecutionContext(
                    agent_id="Pickle",
                    session_id="session-1",
                    workspace_path=workspace,
                    path_policy=WorkspacePathAccessPolicy(),
                    shell_session_manager=None,
                ),
            )
            replace_result = await tool.execute(
                {
                    "path": "note.txt",
                    "action": "replace",
                    "old_text": "world",
                    "new_text": "pickle",
                },
                ToolExecutionContext(
                    agent_id="Pickle",
                    session_id="session-1",
                    workspace_path=workspace,
                    path_policy=WorkspacePathAccessPolicy(),
                    shell_session_manager=None,
                ),
            )

            content = (workspace / "note.txt").read_text(encoding="utf-8")

        self.assertFalse(create_result.is_error)
        self.assertFalse(replace_result.is_error)
        self.assertEqual("hello pickle", content)
        self.assertEqual("replace", replace_result.metadata["action"])


if __name__ == "__main__":
    unittest.main()
