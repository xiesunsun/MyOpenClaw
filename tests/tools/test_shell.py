from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from myopenclaw.tools.base import ToolExecutionContext
from myopenclaw.tools.catalog import builtin_tools
from myopenclaw.tools.registry import ToolRegistry
from myopenclaw.tools.shell import (
    ShellCloseTool,
    ShellExecTool,
    ShellRestartTool,
    ShellSessionManager,
    ShellStatus,
)


class ShellToolTests(unittest.IsolatedAsyncioTestCase):
    def test_builtin_catalog_registers_shell_tools(self) -> None:
        registry = ToolRegistry(tools=builtin_tools())

        tools = registry.resolve_many(["shell_exec", "shell_restart", "shell_close"])

        self.assertEqual(
            ["shell_exec", "shell_restart", "shell_close"],
            [tool.spec.name for tool in tools],
        )

    def test_shell_session_manager_reuses_session_for_same_conversation(self) -> None:
        manager = ShellSessionManager()
        workspace = Path("/tmp/workspace")

        first = manager.get_or_create("session-1", workspace)
        second = manager.get_or_create("session-1", workspace)

        self.assertIs(first, second)
        self.assertEqual(workspace.resolve(), first.workspace_path)

    async def test_shell_exec_reuses_same_shell_and_persists_cwd(self) -> None:
        manager = ShellSessionManager()
        exec_tool = ShellExecTool()

        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "nested").mkdir()
            context = ToolExecutionContext(
                agent_id="Pickle",
                session_id="session-1",
                workspace_path=workspace,
                workspace_files=None,
                shell_session_manager=manager,
            )

            first = await exec_tool.execute({"command": "cd nested"}, context)
            second = await exec_tool.execute({"command": "pwd"}, context)

        self.assertFalse(first.is_error)
        self.assertEqual(str((workspace / "nested").resolve()), first.metadata["cwd"])
        self.assertEqual(True, first.metadata["created_new_shell"])
        self.assertEqual(str((workspace / "nested").resolve()), second.content.strip())
        self.assertEqual(str((workspace / "nested").resolve()), second.metadata["cwd"])
        self.assertEqual("ready", second.metadata["shell_status"])

    async def test_shell_restart_recreates_shell_at_workspace_root(self) -> None:
        manager = ShellSessionManager()
        exec_tool = ShellExecTool()
        restart_tool = ShellRestartTool()

        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "nested").mkdir()
            context = ToolExecutionContext(
                agent_id="Pickle",
                session_id="session-1",
                workspace_path=workspace,
                workspace_files=None,
                shell_session_manager=manager,
            )

            await exec_tool.execute({"command": "cd nested"}, context)
            restart_result = await restart_tool.execute({}, context)
            pwd_result = await exec_tool.execute({"command": "pwd"}, context)

        self.assertFalse(restart_result.is_error)
        self.assertEqual(str(workspace.resolve()), restart_result.metadata["cwd"])
        self.assertEqual("ready", restart_result.metadata["shell_status"])
        self.assertEqual(str(workspace.resolve()), pwd_result.content.strip())

    async def test_shell_close_terminates_session_shell(self) -> None:
        manager = ShellSessionManager()
        exec_tool = ShellExecTool()
        close_tool = ShellCloseTool()

        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            context = ToolExecutionContext(
                agent_id="Pickle",
                session_id="session-1",
                workspace_path=workspace,
                workspace_files=None,
                shell_session_manager=manager,
            )

            await exec_tool.execute({"command": "pwd"}, context)
            close_result = await close_tool.execute({}, context)

        self.assertFalse(close_result.is_error)
        self.assertEqual("terminated", close_result.metadata["shell_status"])
        self.assertIsNone(manager.get("session-1"))

    async def test_shell_exec_returns_structured_metadata(self) -> None:
        manager = ShellSessionManager()
        exec_tool = ShellExecTool()

        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            context = ToolExecutionContext(
                agent_id="Pickle",
                session_id="session-1",
                workspace_path=workspace,
                workspace_files=None,
                shell_session_manager=manager,
            )

            result = await exec_tool.execute({"command": "printf 'hello'"}, context)

        self.assertFalse(result.is_error)
        self.assertEqual("hello", result.content)
        self.assertEqual(0, result.metadata["exit_code"])
        self.assertEqual(False, result.metadata["timed_out"])
        self.assertEqual(False, result.metadata["truncated"])
        self.assertEqual("ready", result.metadata["shell_status"])

    async def test_shell_exec_reports_non_zero_exit_without_killing_shell(self) -> None:
        manager = ShellSessionManager()
        exec_tool = ShellExecTool()

        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            context = ToolExecutionContext(
                agent_id="Pickle",
                session_id="session-1",
                workspace_path=workspace,
                workspace_files=None,
                shell_session_manager=manager,
            )

            failed = await exec_tool.execute({"command": "false"}, context)
            recovered = await exec_tool.execute({"command": "printf ok"}, context)

        self.assertTrue(failed.is_error)
        self.assertEqual(1, failed.metadata["exit_code"])
        self.assertEqual("ready", failed.metadata["shell_status"])
        self.assertEqual("ok", recovered.content)

    def test_shell_status_string_values_are_stable(self) -> None:
        self.assertEqual("ready", ShellStatus.READY)
        self.assertEqual("terminated", ShellStatus.TERMINATED)


if __name__ == "__main__":
    unittest.main()
