import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pickel.tools.base import ToolExecutionContext
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.tools.file_formatter import FileToolFormatter
from pickel.tools.file_service import WorkspaceFileService
from pickel.tools.file_tools import (
    EditTool,
    GlobTool,
    GrepTool,
    LsTool,
    ReadTool,
    WriteTool,
)
from pickel.tools.policy import FullAccessPathPolicy, WorkspacePathAccessPolicy
from pickel.tools.services import ToolServices


class FilesystemToolTests(unittest.IsolatedAsyncioTestCase):
    def _context(
        self, workspace: Path, workspace_files: WorkspaceFileService | None = None
    ) -> ToolExecutionContext:
        return ToolExecutionContext(
            agent_id="Pickle",
            identity=ExecutionIdentity(session_id="session-1"),
            workspace_path=workspace,
            services=ToolServices(
                workspace_files=workspace_files
                or WorkspaceFileService(
                    workspace_root=workspace,
                    access_policy=WorkspacePathAccessPolicy(),
                )
            ),
        )

    async def test_read_tool_marks_character_truncation_in_model_content(self) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "note.txt").write_text("a" * 60_000, encoding="utf-8")
            tool = ReadTool(FileToolFormatter())

            result = await tool.execute(
                {"path": "note.txt"},
                ToolExecutionContext(
                    agent_id="Pickle",
                    identity=ExecutionIdentity(session_id="session-1"),
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
                    identity=ExecutionIdentity(session_id="session-1"),
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

    async def test_read_tool_reports_continuation_and_rejects_offset_past_end(
        self,
    ) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "note.txt").write_text(
                "one\ntwo\nthree\nfour\n", encoding="utf-8"
            )
            tool = ReadTool(FileToolFormatter())
            context = self._context(workspace)

            partial = await tool.execute(
                {"path": "note.txt", "offset": 2, "limit": 2}, context
            )
            past_end = await tool.execute(
                {"path": "note.txt", "offset": 9, "limit": 2}, context
            )

        self.assertIn("Showing lines 2-3", partial.content)
        self.assertIn("offset=4", partial.content)
        self.assertTrue(partial.metadata["truncated"])
        self.assertTrue(past_end.is_error)
        self.assertIn("beyond the end", past_end.content)

    async def test_glob_finds_files_and_marks_result_limit(self) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "nested").mkdir()
            (workspace / "one.py").write_text("", encoding="utf-8")
            (workspace / "nested" / "two.py").write_text("", encoding="utf-8")
            (workspace / "note.txt").write_text("", encoding="utf-8")

            result = await GlobTool(FileToolFormatter()).execute(
                {"pattern": "**/*.py", "limit": 1}, self._context(workspace)
            )

        self.assertFalse(result.is_error)
        self.assertEqual(1, result.metadata["returned_count"])
        self.assertTrue(result.metadata["truncated"])
        self.assertIn("Result limit reached", result.content)

    async def test_grep_supports_case_filter_limit_and_invalid_regex(self) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "one.py").write_text(
                "Needle one\nneedle two\n", encoding="utf-8"
            )
            (workspace / "two.txt").write_text("needle three\n", encoding="utf-8")
            tool = GrepTool(FileToolFormatter())
            context = self._context(workspace)

            result = await tool.execute(
                {
                    "pattern": "needle",
                    "glob": "*.py",
                    "ignore_case": True,
                    "limit": 1,
                },
                context,
            )
            invalid = await tool.execute({"pattern": "["}, context)

        self.assertFalse(result.is_error)
        self.assertEqual(1, result.metadata["returned_count"])
        self.assertTrue(result.metadata["truncated"])
        self.assertIn("one.py:", result.content)
        self.assertNotIn("two.txt", result.content)
        self.assertTrue(invalid.is_error)

    async def test_search_does_not_follow_symlink_outside_workspace(self) -> None:
        with (
            TemporaryDirectory() as workspace_tmpdir,
            TemporaryDirectory() as external_tmpdir,
        ):
            workspace = Path(workspace_tmpdir)
            external = Path(external_tmpdir) / "secret.txt"
            external.write_text("private-token\n", encoding="utf-8")
            (workspace / "linked.txt").symlink_to(external)
            context = self._context(workspace)

            glob_result = await GlobTool(FileToolFormatter()).execute(
                {"pattern": "**/*"}, context
            )
            grep_result = await GrepTool(FileToolFormatter()).execute(
                {"pattern": "private-token"}, context
            )

        self.assertNotIn("linked.txt", glob_result.content)
        self.assertNotIn("private-token", grep_result.content)

    async def test_search_uses_python_fallback_when_ripgrep_is_unavailable(
        self,
    ) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "nested").mkdir()
            (workspace / "nested" / "note.py").write_text(
                "fallback match\n", encoding="utf-8"
            )
            context = self._context(workspace)

            with patch("pickel.tools.file_service.shutil.which", return_value=None):
                glob_result = await GlobTool(FileToolFormatter()).execute(
                    {"pattern": "**/*.py"}, context
                )
                grep_result = await GrepTool(FileToolFormatter()).execute(
                    {"pattern": "fallback", "glob": "*.py"}, context
                )

        self.assertIn("nested/note.py", glob_result.content)
        self.assertIn("nested/note.py:1:fallback match", grep_result.content)

    async def test_grep_skips_binary_files_in_ripgrep_and_fallback(self) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "binary.dat").write_bytes(b"needle\0binary\n")
            context = self._context(workspace)
            tool = GrepTool(FileToolFormatter())

            rg_result = await tool.execute({"pattern": "needle"}, context)
            with patch("pickel.tools.file_service.shutil.which", return_value=None):
                fallback_result = await tool.execute({"pattern": "needle"}, context)

        self.assertEqual("(no matches)", rg_result.content)
        self.assertEqual("(no matches)", fallback_result.content)

    async def test_ripgrep_and_fallback_share_git_ignore_and_hidden_semantics(
        self,
    ) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
            (workspace / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
            for filename in ("keep.py", "ignored.py", ".hidden.py"):
                (workspace / filename).write_text(
                    f"needle in {filename}\n", encoding="utf-8"
                )
            context = self._context(workspace)
            glob_tool = GlobTool(FileToolFormatter())
            grep_tool = GrepTool(FileToolFormatter())

            rg_glob = await glob_tool.execute({"pattern": "**/*.py"}, context)
            rg_all = await glob_tool.execute({"pattern": "**/*"}, context)
            rg_grep = await grep_tool.execute(
                {"pattern": "needle", "glob": "*.py"}, context
            )
            with patch("pickel.tools.file_service.shutil.which", return_value=None):
                fallback_glob = await glob_tool.execute({"pattern": "**/*.py"}, context)
                fallback_all = await glob_tool.execute({"pattern": "**/*"}, context)
                fallback_grep = await grep_tool.execute(
                    {"pattern": "needle", "glob": "*.py"}, context
                )

        self.assertEqual(
            set(rg_glob.content.splitlines()), set(fallback_glob.content.splitlines())
        )
        self.assertEqual(
            set(rg_grep.content.splitlines()), set(fallback_grep.content.splitlines())
        )
        self.assertEqual(
            set(rg_all.content.splitlines()), set(fallback_all.content.splitlines())
        )
        self.assertNotIn("ignored.py", rg_glob.content)
        self.assertIn(".hidden.py", rg_glob.content)
        self.assertNotIn(".git/", rg_all.content)

    async def test_empty_results_are_self_describing(self) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "empty").mkdir()
            (workspace / "empty.txt").write_text("", encoding="utf-8")
            context = self._context(workspace)

            ls_result = await LsTool(FileToolFormatter()).execute(
                {"path": "empty"}, context
            )
            read_result = await ReadTool(FileToolFormatter()).execute(
                {"path": "empty.txt"}, context
            )
            glob_result = await GlobTool(FileToolFormatter()).execute(
                {"pattern": "**/*.py"}, context
            )
            grep_result = await GrepTool(FileToolFormatter()).execute(
                {"pattern": "missing"}, context
            )

        self.assertEqual("(empty directory)", ls_result.content)
        self.assertEqual("(empty file)", read_result.content)
        self.assertEqual("(no matches)", glob_result.content)
        self.assertEqual("(no matches)", grep_result.content)

    async def test_ls_marks_truncation_only_when_more_entries_exist(self) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "one.txt").write_text("", encoding="utf-8")
            tool = LsTool(FileToolFormatter())
            context = self._context(workspace)

            exact = await tool.execute({"limit": 1}, context)
            (workspace / "two.txt").write_text("", encoding="utf-8")
            truncated = await tool.execute({"limit": 1}, context)

        self.assertFalse(exact.metadata["truncated"])
        self.assertTrue(truncated.metadata["truncated"])

    async def test_read_tool_rejects_paths_outside_workspace(self) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            tool = ReadTool(FileToolFormatter())

            result = await tool.execute(
                {"path": "../secret.txt"},
                ToolExecutionContext(
                    agent_id="Pickle",
                    identity=ExecutionIdentity(session_id="session-1"),
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
                    "path": "nested/note.txt",
                    "content": "hello world",
                },
                ToolExecutionContext(
                    agent_id="Pickle",
                    identity=ExecutionIdentity(session_id="session-1"),
                    workspace_path=workspace,
                    services=ToolServices(workspace_files=workspace_files),
                ),
            )
            edit_result = await edit_tool.execute(
                {
                    "path": "nested/note.txt",
                    "old_text": "world",
                    "new_text": "pickle",
                },
                ToolExecutionContext(
                    agent_id="Pickle",
                    identity=ExecutionIdentity(session_id="session-1"),
                    workspace_path=workspace,
                    services=ToolServices(workspace_files=workspace_files),
                ),
            )

            content = (workspace / "nested" / "note.txt").read_text(encoding="utf-8")

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
                    identity=ExecutionIdentity(session_id="session-1"),
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
                    identity=ExecutionIdentity(session_id="session-1"),
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
