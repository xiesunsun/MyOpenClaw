import asyncio
import os
import signal
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pickel.tools.base import ToolExecutionContext, ToolExecutionError
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.tools.bus import ToolBus
from pickel.tools.catalog import install_builtin_tools
from pickel.tools.services import ToolServices
from pickel.tools.shell import (
    BashSession,
    BashTool,
    LocalBashOperations,
    ShellExecutionResult,
    ShellStatus,
    _dangerous_command_reason,
)


class _RecordingBash:
    def __init__(self) -> None:
        self.calls = []

    async def exec(self, **kwargs) -> ShellExecutionResult:
        self.calls.append(kwargs)
        return ShellExecutionResult(
            stdout="remote-ok",
            stderr="",
            exit_code=0,
            cwd=kwargs["workspace_path"],
            shell_status=ShellStatus.READY,
            environment="staging",
        )

    def close(self, session_id: str) -> None:
        pass


class ShellToolTests(unittest.IsolatedAsyncioTestCase):
    def test_builtin_catalog_registers_shell_tools(self) -> None:
        bus = ToolBus()
        install_builtin_tools(bus)

        self.assertEqual("bash", bus.get("bash").tool.spec.name)
        for legacy_name in (
            "shell_exec",
            "shell_wait",
            "shell_stdin",
            "shell_interrupt",
            "shell_tasks",
            "shell_output",
            "shell_kill",
            "shell_restart",
            "shell_close",
        ):
            self.assertNotIn(legacy_name, bus.list_names())

    async def test_bash_contract_does_not_depend_on_local_pty(self) -> None:
        bash = _RecordingBash()
        context = ToolExecutionContext(
            agent_id="Pickle",
            identity=ExecutionIdentity(session_id="session-1"),
            workspace_path=Path("/tmp/workspace"),
            services=ToolServices(bash=bash),
        )

        result = await BashTool().execute(
            {"command": "echo hi", "timeout": 2.5}, context
        )

        self.assertEqual("remote-ok", result["stdout"])
        self.assertEqual("echo hi", bash.calls[0]["command"])
        self.assertEqual(2.5, bash.calls[0]["timeout"])
        self.assertEqual("staging", result["environment"])

    async def test_bash_uses_replaceable_operations_and_persists_cwd(self) -> None:
        manager = LocalBashOperations()
        bash = manager
        tool = BashTool()

        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "nested").mkdir()
            context = ToolExecutionContext(
                agent_id="Pickle",
                identity=ExecutionIdentity(session_id="session-1"),
                workspace_path=workspace,
                services=ToolServices(bash=bash),
            )
            try:
                await tool.execute({"command": "cd nested"}, context)
                second = await tool.execute({"command": "pwd"}, context)
            finally:
                bash.close(context.identity.session_id)

        self.assertEqual(
            str((workspace / "nested").resolve()), second["stdout"].strip()
        )
        self.assertEqual(
            str((workspace / "nested").resolve()),
            second["cwd"],
        )

    async def test_bash_nonzero_exit_is_command_result_not_tool_error(self) -> None:
        manager = LocalBashOperations()
        bash = manager
        tool = BashTool()

        with TemporaryDirectory() as tmpdir:
            context = ToolExecutionContext(
                agent_id="Pickle",
                identity=ExecutionIdentity(session_id="session-1"),
                workspace_path=Path(tmpdir),
                services=ToolServices(bash=bash),
            )
            try:
                result = await tool.execute(
                    {"command": "exit_code=7; (exit $exit_code)"}, context
                )
            finally:
                bash.close(context.identity.session_id)

        self.assertEqual(7, result["exit_code"])
        rendered = tool.render(result)
        self.assertIn("exit_code=7", rendered[0].text)

    async def test_bash_timeout_stops_foreground_and_keeps_shell_usable(self) -> None:
        manager = LocalBashOperations()
        bash = manager
        tool = BashTool()

        with TemporaryDirectory() as tmpdir:
            context = ToolExecutionContext(
                agent_id="Pickle",
                identity=ExecutionIdentity(session_id="session-1"),
                workspace_path=Path(tmpdir),
                services=ToolServices(bash=bash),
            )
            try:
                timed_out = await tool.execute(
                    {"command": "echo before; sleep 30", "timeout": 0.1}, context
                )
                after = await tool.execute({"command": "echo alive"}, context)
            finally:
                bash.close(context.identity.session_id)

        self.assertTrue(timed_out["timed_out"])
        self.assertEqual(124, timed_out["exit_code"])
        self.assertIn("alive", after["stdout"])


class BashSessionBehaviorTests(unittest.TestCase):
    def test_history_expansion_is_disabled_but_background_pid_expands(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shell = BashSession(workspace_path=Path(tmpdir))
            try:
                result = shell.exec('sleep 0.1 & echo "bg pid: $!"')
                pid = int(result.stdout.removeprefix("bg pid: "))
                shell.exec(f'wait {pid}; echo "wait status: $?"')
            finally:
                shell.terminate()

        self.assertGreater(pid, 0)
        self.assertNotIn("event not found", result.stdout)
        self.assertEqual(0, result.exit_code)

    def test_syntax_error_returns_exit_two_and_session_stays_ready(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shell = BashSession(workspace_path=Path(tmpdir))
            try:
                failed = shell.exec("if then; fi", timeout_ms=1_000)
                after = shell.exec("echo alive")
            finally:
                shell.terminate()

        self.assertEqual(2, failed.exit_code)
        self.assertIs(ShellStatus.READY, failed.shell_status)
        self.assertFalse(failed.timed_out)
        self.assertIn("syntax error", failed.stdout)
        self.assertEqual("alive", after.stdout)

    def test_close_terminates_background_job(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shell = BashSession(workspace_path=Path(tmpdir))
            result = shell.exec("sleep 30 & printf '%s\\n' $!")
            pid = int(result.stdout)
            try:
                shell.terminate()
                deadline = time.monotonic() + 1
                while _process_exists(pid) and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertFalse(_process_exists(pid))
            finally:
                if _process_exists(pid):
                    os.kill(pid, signal.SIGKILL)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


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
            shell = BashSession(
                workspace_path=workspace,
                output_dir=out_dir,
                limits=OutputLimits(
                    raw_max_chars=100_000,
                    result_max_chars=200,
                    head_chars=120,
                    tail_chars=50,
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
            shell = BashSession(workspace_path=Path(tmpdir))
            try:
                result = shell.exec("echo short")
            finally:
                shell.terminate()

        self.assertFalse(result.truncated)
        self.assertIsNone(result.full_output_path)


class StderrSeparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_stderr_is_separated_from_stdout(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shell = BashSession(workspace_path=Path(tmpdir))
            try:
                result = shell.exec("echo out-line; echo err-line >&2")
            finally:
                shell.terminate()

        self.assertIn("out-line", result.stdout)
        self.assertNotIn("err-line", result.stdout)
        self.assertIn("err-line", result.stderr)

    async def test_tool_content_appends_stderr_block(self) -> None:
        manager = LocalBashOperations()
        tool = BashTool()
        with TemporaryDirectory() as tmpdir:
            context = ToolExecutionContext(
                agent_id="Pickle",
                identity=ExecutionIdentity(session_id="session-1"),
                workspace_path=Path(tmpdir),
                services=ToolServices(bash=manager),
            )
            try:
                result = await tool.execute(
                    {"command": "echo ok; echo bad >&2"}, context
                )
            finally:
                manager.close(context.identity.session_id)

        self.assertIn("ok", result["stdout"])
        self.assertIn("bad", result["stderr"])


class TimeoutKeepsSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_returns_partial_output_and_keeps_session(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shell = BashSession(workspace_path=Path(tmpdir))
            try:
                result = shell.exec("echo before-sleep; sleep 30", timeout_ms=500)

                self.assertTrue(result.timed_out)
                self.assertEqual(ShellStatus.RUNNING, result.shell_status)
                self.assertIn("before-sleep", result.stdout)
                self.assertTrue(shell.is_alive())  # 会话没被杀
                self.assertTrue(shell.pending)
            finally:
                shell.terminate()

    async def test_exec_while_pending_raises(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shell = BashSession(workspace_path=Path(tmpdir))
            try:
                shell.exec("sleep 30", timeout_ms=300)
                with self.assertRaises(RuntimeError):
                    shell.exec("echo nope")
            finally:
                shell.terminate()


class EventLoopNotBlockedTests(unittest.IsolatedAsyncioTestCase):
    async def test_exec_does_not_block_event_loop(self) -> None:
        manager = LocalBashOperations()
        tool = BashTool()
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.05)
                ticks += 1

        with TemporaryDirectory() as tmpdir:
            context = ToolExecutionContext(
                agent_id="Pickle",
                identity=ExecutionIdentity(session_id="session-1"),
                workspace_path=Path(tmpdir),
                services=ToolServices(bash=manager),
            )
            ticker_task = asyncio.create_task(ticker())
            try:
                await tool.execute({"command": "sleep 0.6; echo done"}, context)
                ticks_during = ticks
            finally:
                ticker_task.cancel()
                manager.close(context.identity.session_id)

        self.assertGreaterEqual(ticks_during, 8)


class DangerousCommandTests(unittest.IsolatedAsyncioTestCase):
    # 拦截判定测纯函数，零执行风险；漏拦时下面的工具级用例
    # 会命中 _NoShellManager 的 AssertionError，同样不会执行任何命令。
    def test_reason_blocks_rm_rf_root_and_home(self) -> None:
        for cmd in (
            "rm -rf /",
            "rm -fr /",
            "sudo rm -rf /*",
            "rm -rf ~",
            "rm -rf $HOME",
        ):
            self.assertIsNotNone(_dangerous_command_reason(cmd), cmd)

    def test_reason_blocks_mkfs_dd_forkbomb_chmod(self) -> None:
        for cmd in (
            "mkfs.ext4 /dev/sda1",
            "dd if=/dev/zero of=/dev/sda",
            ":(){ :|:& };:",
            "chmod -R 777 /",
        ):
            self.assertIsNotNone(_dangerous_command_reason(cmd), cmd)

    def test_reason_allows_normal_rm_and_quoted_text(self) -> None:
        for cmd in (
            "rm -rf ./build",
            "rm -rf node_modules",
            "echo 'rm -rf /' 只是文本",
        ):
            self.assertIsNone(_dangerous_command_reason(cmd), cmd)

    async def test_tool_blocks_before_touching_shell(self) -> None:
        class FailingBash:
            async def exec(self, **kwargs):
                raise AssertionError("dangerous command reached BashOperations")

            def close(self, session_id: str) -> None:
                pass

        tool = BashTool()
        with TemporaryDirectory() as tmpdir:
            context = ToolExecutionContext(
                agent_id="Pickle",
                identity=ExecutionIdentity(session_id="session-1"),
                workspace_path=Path(tmpdir),
                services=ToolServices(bash=FailingBash()),
            )
            with self.assertRaisesRegex(ToolExecutionError, "blocked"):
                await tool.execute({"command": "rm -rf /"}, context)

    async def test_tool_allows_harmless_rm_in_workspace(self) -> None:
        manager = LocalBashOperations()
        tool = BashTool()
        with TemporaryDirectory() as tmpdir:
            context = ToolExecutionContext(
                agent_id="Pickle",
                identity=ExecutionIdentity(session_id="session-1"),
                workspace_path=Path(tmpdir),
                services=ToolServices(bash=manager),
            )
            try:
                await tool.execute({"command": "mkdir -p ./build"}, context)
                await tool.execute({"command": "rm -rf ./build"}, context)
            finally:
                manager.close(context.identity.session_id)
