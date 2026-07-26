from pathlib import Path
import unittest

from pickel.tools.sandbox import SandboxPolicy, SandboxSettings


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
