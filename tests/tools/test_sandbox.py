from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from pickel.tools.sandbox import (
    SandboxPolicy,
    SandboxSettings,
    SandboxUnavailableError,
)


def _policy(**kwargs) -> SandboxPolicy:
    return SandboxPolicy.from_settings(
        SandboxSettings(**kwargs),
        home=Path("/home/u/.pickel"),
        project_root=Path("/proj"),
    )


class EnvFilterTests(unittest.TestCase):
    def test_default_patterns_strip_credential_shaped_names(self) -> None:
        policy = _policy()

        filtered = policy.filter_env({
            "PATH": "/usr/bin",
            "OPENVIKING_API_KEY": "secret",
            "github_token": "secret",
            "MY_SECRET": "secret",
            "DB_PASSWORD": "secret",
            "GOOGLE_APPLICATION_CREDENTIALS": "/path",
            "AWS_ACCESS_KEY_ID": "secret",
            "HOME": "/home/u",
        })

        self.assertEqual({"PATH": "/usr/bin", "HOME": "/home/u"}, filtered)

    def test_env_deny_adds_exact_names(self) -> None:
        policy = _policy(env_deny=["MY_PLAIN_VAR"])

        filtered = policy.filter_env({"MY_PLAIN_VAR": "x", "KEEP": "y"})

        self.assertEqual({"KEEP": "y"}, filtered)

    def test_env_allow_exempts_from_default_patterns(self) -> None:
        policy = _policy(env_allow=["GITHUB_TOKEN"])

        filtered = policy.filter_env({"GITHUB_TOKEN": "t", "OTHER_TOKEN": "x"})

        self.assertEqual({"GITHUB_TOKEN": "t"}, filtered)

    def test_disabled_policy_keeps_everything(self) -> None:
        policy = _policy(enabled=False)
        env = {"OPENVIKING_API_KEY": "secret", "PATH": "/usr/bin"}

        self.assertEqual(env, policy.filter_env(env))


class WrapCommandTests(unittest.TestCase):
    def _policy_for(self, tmp: Path, **kwargs) -> SandboxPolicy:
        return SandboxPolicy.from_settings(
            SandboxSettings(**kwargs), home=tmp / "home", project_root=tmp / "proj"
        )

    def _wrap(self, policy: SandboxPolicy, workspace: Path) -> tuple[list[str], bool]:
        return policy.wrap_command(["/bin/bash", "-s"], workspace=workspace)

    def test_wrapped_command_has_required_flags_and_binds(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = tmp / "ws"
            workspace.mkdir()

            with mock.patch("shutil.which", return_value="/usr/bin/bwrap"):
                argv, sandboxed = self._wrap(self._policy_for(tmp), workspace)

            self.assertTrue(sandboxed)
            self.assertEqual("bwrap", argv[0])
            # --new-session 必需：缺它 bwrap 内 job control 失效
            self.assertIn("--new-session", argv)
            self.assertIn("--die-with-parent", argv)
            self.assertIn("--dev", argv)
            self.assertNotIn("--dev-bind", argv)
            joined = " ".join(argv)
            self.assertIn("--ro-bind / /", joined)
            self.assertIn(f"--bind {workspace} {workspace}", joined)
            self.assertIn("--bind /tmp /tmp", joined)
            self.assertEqual(["/bin/bash", "-s"], argv[argv.index("--") + 1 :])

    def test_workspace_bind_precedes_self_protect_ro_bind(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = tmp / "ws"
            (workspace / "src" / "pickel").mkdir(parents=True)
            (workspace / "agents").mkdir()
            policy = SandboxPolicy.from_settings(
                SandboxSettings(), home=tmp / "home", project_root=workspace
            )

            with mock.patch("shutil.which", return_value="/usr/bin/bwrap"):
                argv, _ = self._wrap(policy, workspace)

            joined = " ".join(argv)
            bind_at = joined.index(f"--bind {workspace} {workspace}")
            ro_at = joined.index(f"--ro-bind {workspace / 'agents'}")
            self.assertLess(bind_at, ro_at, "self-protect 必须在 workspace bind 之后盖回")

    def test_deny_read_paths_become_tmpfs(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = tmp / "ws"
            workspace.mkdir()
            secret = tmp / "secret-dir"
            secret.mkdir()
            policy = self._policy_for(tmp, deny_read=[str(secret)])

            with mock.patch("shutil.which", return_value="/usr/bin/bwrap"):
                argv, _ = self._wrap(policy, workspace)

            self.assertIn(f"--tmpfs {secret}", " ".join(argv))

    def test_missing_paths_are_skipped(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = tmp / "ws"
            workspace.mkdir()
            policy = self._policy_for(tmp, deny_read=[str(tmp / "nope")])

            with mock.patch("shutil.which", return_value="/usr/bin/bwrap"):
                argv, _ = self._wrap(policy, workspace)

            self.assertNotIn("nope", " ".join(argv))

    def test_allow_write_paths_are_bound_writable(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            workspace = tmp / "ws"
            workspace.mkdir()
            extra = tmp / "cache"
            extra.mkdir()
            policy = self._policy_for(tmp, allow_write=[str(extra)])

            with mock.patch("shutil.which", return_value="/usr/bin/bwrap"):
                argv, _ = self._wrap(policy, workspace)

            self.assertIn(f"--bind {extra} {extra}", " ".join(argv))

    def test_disabled_policy_returns_command_unchanged(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            policy = self._policy_for(tmp, enabled=False)

            argv, sandboxed = self._wrap(policy, tmp)

            self.assertEqual(["/bin/bash", "-s"], argv)
            self.assertFalse(sandboxed)

    def test_missing_bwrap_degrades_when_not_strict(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            policy = self._policy_for(tmp)

            with mock.patch("shutil.which", return_value=None):
                argv, sandboxed = self._wrap(policy, tmp)

            self.assertEqual(["/bin/bash", "-s"], argv)
            self.assertFalse(sandboxed)

    def test_missing_bwrap_raises_when_strict(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            policy = self._policy_for(tmp, strict=True)

            with mock.patch("shutil.which", return_value=None):
                with self.assertRaises(SandboxUnavailableError):
                    self._wrap(policy, tmp)
