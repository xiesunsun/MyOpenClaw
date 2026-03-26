import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_module_entrypoint_runs_without_pythonpath(self) -> None:
        result = subprocess.run(
            ["uv", "run", "python", "-m", "myopenclaw.interfaces.cli.main", "--help"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage:", result.stdout)
