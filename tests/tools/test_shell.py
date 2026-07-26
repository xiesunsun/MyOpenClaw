import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from pickel.tools.services import ToolServices
from pickel.tools.base import ToolExecutionContext
from pickel.tools.catalog import builtin_tools, install_builtin_tools
from pickel.tools.bus import ToolBus
from pickel.tools.shell import (
    ShellCloseTool,
    ShellExecTool,
    PersistentShell,
    ShellRestartTool,
    ShellSessionManager,
    ShellStatus,
)


class ShellToolTests(unittest.IsolatedAsyncioTestCase):
    def test_builtin_catalog_registers_shell_tools(self) -> None:
        bus = ToolBus()
        install_builtin_tools(bus)

        tools = [
            bus.get(name).tool
            for name in ["shell_exec", "shell_restart", "shell_close"]
        ]

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

    def test_persistent_shell_defaults_to_two_minute_timeout(self) -> None:
        shell = PersistentShell(workspace_path=Path("/tmp/workspace"))

        self.assertEqual(120000, shell.default_timeout_ms)

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
                services=ToolServices(shell_sessions=manager),
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
                services=ToolServices(shell_sessions=manager),
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
                services=ToolServices(shell_sessions=manager),
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
                services=ToolServices(shell_sessions=manager),
            )

            result = await exec_tool.execute({"command": "printf 'hello'"}, context)

        self.assertFalse(result.is_error)
        self.assertEqual("hello", result.content)
        self.assertEqual(0, result.metadata["exit_code"])
        self.assertEqual(False, result.metadata["timed_out"])
        self.assertEqual(False, result.metadata["truncated"])
        self.assertEqual("ready", result.metadata["shell_status"])

    async def test_shell_exec_does_not_truncate_long_output(self) -> None:
        manager = ShellSessionManager()
        exec_tool = ShellExecTool()

        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            context = ToolExecutionContext(
                agent_id="Pickle",
                session_id="session-1",
                workspace_path=workspace,
                services=ToolServices(shell_sessions=manager),
            )

            result = await exec_tool.execute({"command": "python -c \"print('a' * 5000, end='')\""}, context)

        self.assertFalse(result.is_error)
        self.assertEqual("a" * 5000, result.content)
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
                services=ToolServices(shell_sessions=manager),
            )

            failed = await exec_tool.execute({"command": "false"}, context)
            recovered = await exec_tool.execute({"command": "printf ok"}, context)

        self.assertTrue(failed.is_error)
        self.assertEqual(1, failed.metadata["exit_code"])
        self.assertEqual("ready", failed.metadata["shell_status"])
        self.assertEqual("ok", recovered.content)

    async def test_shell_exec_allows_timeout_override(self) -> None:
        manager = ShellSessionManager()
        exec_tool = ShellExecTool()

        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            context = ToolExecutionContext(
                agent_id="Pickle",
                session_id="session-1",
                workspace_path=workspace,
                services=ToolServices(shell_sessions=manager),
            )

            timed_out = await exec_tool.execute(
                {"command": "sleep 1", "timeout_ms": 50},
                context,
            )
            # 新语义：超时不杀会话，前台命令仍在跑
            session = manager.get("session-1")
            self.assertTrue(session.shell.is_alive())
            self.assertTrue(session.shell.pending)
            manager.close("session-1")

        self.assertTrue(timed_out.is_error)
        self.assertIn("timed out and is still running", timed_out.content)
        self.assertEqual(124, timed_out.metadata["exit_code"])
        self.assertEqual(True, timed_out.metadata["timed_out"])
        self.assertEqual("running", timed_out.metadata["shell_status"])

    async def test_shell_exec_rejects_non_positive_timeout_override(self) -> None:
        manager = ShellSessionManager()
        exec_tool = ShellExecTool()

        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            context = ToolExecutionContext(
                agent_id="Pickle",
                session_id="session-1",
                workspace_path=workspace,
                services=ToolServices(shell_sessions=manager),
            )

            result = await exec_tool.execute(
                {"command": "pwd", "timeout_ms": 0},
                context,
            )

        self.assertTrue(result.is_error)
        self.assertEqual("timeout_ms must be a positive integer.", result.content)
        self.assertEqual("error", result.metadata["shell_status"])

    def test_shell_status_string_values_are_stable(self) -> None:
        self.assertEqual("ready", ShellStatus.READY)
        self.assertEqual("terminated", ShellStatus.TERMINATED)


if __name__ == "__main__":
    unittest.main()


def _context(workspace: Path, manager: ShellSessionManager) -> ToolExecutionContext:
    return ToolExecutionContext(
        agent_id="Pickle",
        session_id="session-1",
        workspace_path=workspace,
        services=ToolServices(shell_sessions=manager),
    )


class NormalizeOutputTests(unittest.TestCase):
    def test_strips_csi_color_sequences(self) -> None:
        from pickel.tools.shell import _normalize_output

        raw = "\x1b[01;34mdir\x1b[0m\nplain"
        self.assertEqual("dir\nplain", _normalize_output(raw))

    def test_strips_osc_title_sequences(self) -> None:
        from pickel.tools.shell import _normalize_output

        raw = "\x1b]0;window-title\x07hello"
        self.assertEqual("hello", _normalize_output(raw))

    def test_strips_private_mode_sequences(self) -> None:
        from pickel.tools.shell import _normalize_output

        raw = "\x1b[?25lhello\x1b[?25h"
        self.assertEqual("hello", _normalize_output(raw))

    def test_plain_text_untouched(self) -> None:
        from pickel.tools.shell import _normalize_output

        self.assertEqual("a\nb", _normalize_output("a\r\nb\r\n"))


class OutputLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_output_truncates_and_writes_full_file(self) -> None:
        from pickel.tools.shell import OutputLimits

        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            out_dir = workspace / ".pickel" / "shell-output" / "s1"
            shell = PersistentShell(
                workspace_path=workspace,
                output_dir=out_dir,
                limits=OutputLimits(
                    raw_max_chars=100_000, result_max_chars=200, head_chars=120, tail_chars=50
                ),
            )
            try:
                result = shell.exec("seq 1 500")
            finally:
                shell.terminate()

            self.assertTrue(result.truncated)
            self.assertIn("truncated", result.stdout)
            self.assertIsNotNone(result.full_output_path)
            full = result.full_output_path.read_text(encoding="utf-8")
            self.assertIn("500", full)
            self.assertLess(len(result.stdout), 400)

    async def test_short_output_not_truncated(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shell = PersistentShell(workspace_path=Path(tmpdir))
            try:
                result = shell.exec("echo short")
            finally:
                shell.terminate()

        self.assertFalse(result.truncated)
        self.assertIsNone(result.full_output_path)


class StderrSeparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_stderr_is_separated_from_stdout(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shell = PersistentShell(workspace_path=Path(tmpdir))
            try:
                result = shell.exec("echo out-line; echo err-line >&2")
            finally:
                shell.terminate()

        self.assertIn("out-line", result.stdout)
        self.assertNotIn("err-line", result.stdout)
        self.assertIn("err-line", result.stderr)

    async def test_tool_content_appends_stderr_block(self) -> None:
        manager = ShellSessionManager()
        tool = ShellExecTool()
        with TemporaryDirectory() as tmpdir:
            context = _context(Path(tmpdir), manager)
            try:
                result = await tool.execute(
                    {"command": "echo ok; echo bad >&2"}, context
                )
            finally:
                manager.close(context.session_id)

        self.assertIn("ok", result.content)
        self.assertIn("--- stderr ---", result.content)
        self.assertIn("bad", result.content)


class TimeoutKeepsSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_returns_partial_output_and_keeps_session(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shell = PersistentShell(workspace_path=Path(tmpdir))
            try:
                result = shell.exec("echo before-sleep; sleep 30", timeout_ms=500)

                self.assertTrue(result.timed_out)
                self.assertEqual(ShellStatus.RUNNING, result.shell_status)
                self.assertIn("before-sleep", result.stdout)
                self.assertTrue(shell.is_alive())      # 会话没被杀
                self.assertTrue(shell.pending)
            finally:
                shell.terminate()

    async def test_exec_while_pending_raises(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shell = PersistentShell(workspace_path=Path(tmpdir))
            try:
                shell.exec("sleep 30", timeout_ms=300)
                with self.assertRaises(RuntimeError):
                    shell.exec("echo nope")
            finally:
                shell.terminate()


class ForegroundInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_picks_up_completion_after_timeout(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shell = PersistentShell(workspace_path=Path(tmpdir))
            try:
                first = shell.exec("sleep 1; echo late-done", timeout_ms=200)
                self.assertTrue(first.timed_out)

                second = shell.wait_foreground(timeout_ms=3000)

                self.assertEqual(ShellStatus.READY, second.shell_status)
                self.assertIn("late-done", second.stdout)
                self.assertFalse(shell.pending)
            finally:
                shell.terminate()

    async def test_stdin_feeds_interactive_read(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shell = PersistentShell(workspace_path=Path(tmpdir))
            try:
                first = shell.exec("read -r line; echo got:$line", timeout_ms=300)
                self.assertTrue(first.timed_out)

                # write_stdin 的观察窗内命令可能已完成（READY）；
                # 未完成（RUNNING）才继续 wait_foreground
                final = shell.write_stdin("hello-stdin")
                if final.shell_status is ShellStatus.RUNNING:
                    final = shell.wait_foreground(timeout_ms=2000)

                self.assertEqual(ShellStatus.READY, final.shell_status)
                self.assertIn("got:hello-stdin", final.stdout)
            finally:
                shell.terminate()

    async def test_interrupt_recovers_ready_session(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shell = PersistentShell(workspace_path=Path(tmpdir))
            try:
                first = shell.exec("sleep 60", timeout_ms=200)
                self.assertTrue(first.timed_out)

                result = shell.interrupt_foreground()

                self.assertEqual(ShellStatus.READY, result.shell_status)
                self.assertTrue(shell.is_alive())
                self.assertFalse(shell.pending)
                follow_up = shell.exec("echo alive-again")
                self.assertIn("alive-again", follow_up.stdout)
            finally:
                shell.terminate()

    async def test_wait_without_pending_is_error(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shell = PersistentShell(workspace_path=Path(tmpdir))
            try:
                shell.start()
                with self.assertRaises(RuntimeError):
                    shell.wait_foreground(timeout_ms=100)
            finally:
                shell.terminate()


class EventLoopNotBlockedTests(unittest.IsolatedAsyncioTestCase):
    async def test_exec_does_not_block_event_loop(self) -> None:
        manager = ShellSessionManager()
        tool = ShellExecTool()
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        with TemporaryDirectory() as tmpdir:
            context = _context(Path(tmpdir), manager)
            ticker_task = asyncio.create_task(ticker())
            try:
                await tool.execute({"command": "sleep 0.6; echo done"}, context)
                # exec 结束瞬间采样：若 exec 阻塞 loop，ticker 此刻还没跑过几次
                ticks_during = ticks
            finally:
                ticker_task.cancel()
                manager.close(context.session_id)

        self.assertGreaterEqual(ticks_during, 8)
