from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from pickel.tools.sandbox import SandboxPolicy, SandboxSettings
from pickel.tools.shell import PersistentShell, ShellStatus

HAS_BWRAP = shutil.which("bwrap") is not None


def _policy(tmp: Path, **kwargs) -> SandboxPolicy:
    return SandboxPolicy.from_settings(
        SandboxSettings(**kwargs), home=Path.home() / ".pickel", project_root=tmp
    )


class SandboxSpawnTests(unittest.IsolatedAsyncioTestCase):
    async def test_env_is_filtered_even_without_bwrap(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            shell = PersistentShell(workspace_path=tmp, sandbox=_policy(tmp))
            try:
                with mock.patch.dict(
                    "os.environ", {"PROBE_API_KEY": "leak", "PROBE_PLAIN": "fine"}
                ):
                    with mock.patch("shutil.which", return_value=None):
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
class SandboxedShellIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def _shell(self, tmp: Path) -> PersistentShell:
        return PersistentShell(workspace_path=tmp, sandbox=_policy(tmp))

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
        """--new-session 的回归闸：缺它这条必挂。"""
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
                after = shell.exec("echo alive")
                self.assertIn("alive", after.stdout)
            finally:
                shell.terminate()

    async def test_stderr_separation_survives_sandbox(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            shell = self._shell(tmp)
            try:
                shell.start()
                result = shell.exec("echo out; echo err >&2")
                self.assertIn("out", result.stdout)
                self.assertIn("err", result.stderr)
                self.assertNotIn("err", result.stdout)
            finally:
                shell.terminate()
