import platform
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from uuid import uuid4

from pickel.tools.base import ToolExecutionContext
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.tools.sandbox import SandboxPolicy, SandboxSettings
from pickel.tools.services import ToolServices
from pickel.tools.shell import (
    BashSession,
    BashTool,
    LocalBashOperations,
    ShellStatus,
)

HAS_BWRAP = shutil.which("bwrap") is not None
HAS_SEATBELT = platform.system() == "Darwin" and Path("/usr/bin/sandbox-exec").is_file()
HAS_OS_SANDBOX = HAS_BWRAP or HAS_SEATBELT


def _policy(tmp: Path, **kwargs) -> SandboxPolicy:
    return SandboxPolicy.from_settings(
        SandboxSettings(**kwargs), home=Path.home() / ".pickel", project_root=tmp
    )


class SandboxSpawnTests(unittest.IsolatedAsyncioTestCase):
    async def test_env_is_filtered_even_without_os_sandbox(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            shell = BashSession(workspace_path=tmp, sandbox=_policy(tmp))
            try:
                with mock.patch.dict(
                    "os.environ", {"PROBE_API_KEY": "leak", "PROBE_PLAIN": "fine"}
                ):
                    with (
                        mock.patch("platform.system", return_value="Linux"),
                        mock.patch("shutil.which", return_value=None),
                    ):
                        shell.start()
                result = shell.exec(
                    "echo key=[${PROBE_API_KEY:-empty}] plain=$PROBE_PLAIN"
                )
                self.assertIn("key=[empty]", result.stdout)
                self.assertIn("plain=fine", result.stdout)
                self.assertFalse(shell.process.sandboxed)
            finally:
                shell.terminate()


@unittest.skipUnless(HAS_BWRAP, "bubblewrap not installed")
class BubblewrapShellIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _shell(self, tmp: Path) -> BashSession:
        return BashSession(workspace_path=tmp, sandbox=_policy(tmp))

    async def test_workspace_is_writable_and_system_is_not(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            shell = self._shell(tmp)
            try:
                shell.start()
                self.assertTrue(shell.process.sandboxed)
                ok = shell.exec("touch ./probe && echo write-ok")
                self.assertIn("write-ok", ok.stdout)
                denied = shell.exec(
                    "touch /usr/probe 2>/dev/null && echo BAD || echo denied"
                )
                self.assertIn("denied", denied.stdout)
            finally:
                shell.terminate()

    async def test_pickel_home_is_masked(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            shell = self._shell(tmp)
            try:
                shell.start()
                result = shell.exec("ls ~/.pickel 2>/dev/null | wc -l")
                self.assertEqual("0", result.stdout.strip())
            finally:
                shell.terminate()

    async def test_job_control_survives_sandbox(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            shell = self._shell(tmp)
            try:
                shell.start()
                flags = shell.exec("echo flags:$-")
                self.assertIn("m", flags.stdout.split("flags:")[1])

                timed_out = shell.exec("sleep 30", timeout_ms=300)
                self.assertIs(ShellStatus.RUNNING, timed_out.shell_status)
                interrupted = shell.interrupt_foreground()
                self.assertIs(ShellStatus.READY, interrupted.shell_status)
                self.assertTrue(shell.is_alive())
            finally:
                shell.terminate()

    async def test_stderr_separation_survives_sandbox(self) -> None:
        with TemporaryDirectory() as tmpdir:
            shell = self._shell(Path(tmpdir))
            try:
                shell.start()
                result = shell.exec("echo out; echo err >&2")
                self.assertIn("out", result.stdout)
                self.assertIn("err", result.stderr)
                self.assertNotIn("err", result.stdout)
            finally:
                shell.terminate()


@unittest.skipUnless(HAS_SEATBELT, "macOS Seatbelt not available")
class SeatbeltShellIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_workspace_write_and_sensitive_read_boundaries(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            workspace = root / "workspace"
            workspace.mkdir()
            secret = root / "secret"
            secret.mkdir()
            (secret / "value").write_text("hidden", encoding="utf-8")
            policy = SandboxPolicy.from_settings(
                SandboxSettings(strict=True, deny_read=[str(secret)]),
                home=Path.home() / ".pickel",
                project_root=root,
            )
            shell = BashSession(workspace_path=workspace, sandbox=policy)
            outside = Path.home() / f".pickel-seatbelt-write-probe-{uuid4().hex}"
            try:
                shell.start()
                self.assertTrue(shell.process.sandboxed)

                writable = shell.exec("touch allowed && echo write-ok")
                denied_write = shell.exec(
                    f"touch {outside} 2>/dev/null || echo write-denied"
                )
                denied_read = shell.exec(
                    f"cat {secret / 'value'} 2>/dev/null || echo read-denied"
                )

                self.assertIn("write-ok", writable.stdout)
                self.assertIn("write-denied", denied_write.stdout)
                self.assertIn("read-denied", denied_read.stdout)
            finally:
                shell.terminate()
                outside.unlink(missing_ok=True)

    async def test_python_stderr_and_persistent_shell_survive_seatbelt(self) -> None:
        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            shell = BashSession(
                workspace_path=workspace,
                sandbox=_policy(workspace, strict=True),
            )
            try:
                first = shell.exec(
                    "python -c \"import os,sys; print(os.getcwd()); print('err', file=sys.stderr)\""
                )
                timed_out = shell.exec("sleep 30", timeout_ms=200)
                interrupted = shell.interrupt_foreground()
                after = shell.exec("echo alive")

                self.assertEqual(str(workspace.resolve()), first.stdout.strip())
                self.assertEqual("err", first.stderr.strip())
                self.assertTrue(timed_out.timed_out)
                self.assertIs(ShellStatus.READY, interrupted.shell_status)
                self.assertIn("alive", after.stdout)
            finally:
                shell.terminate()


class SandboxMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_exec_metadata_reports_sandbox_state(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            manager = LocalBashOperations(sandbox=_policy(tmp))
            context = ToolExecutionContext(
                agent_id="Pickle",
                identity=ExecutionIdentity(session_id="s"),
                workspace_path=tmp,
                services=ToolServices(bash=manager),
            )
            try:
                result = await BashTool().execute({"command": "echo hi"}, context)

                self.assertEqual(HAS_OS_SANDBOX, result["sandboxed"])
            finally:
                manager.close(context.identity.session_id)
