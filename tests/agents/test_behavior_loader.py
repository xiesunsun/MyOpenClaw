from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from myopenclaw.agents.behavior_loader import BehaviorLoader


class BehaviorLoaderTests(unittest.TestCase):
    def test_load_reads_agent_markdown_from_directory(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            behavior_dir = root / "Pickle"
            behavior_dir.mkdir()
            (behavior_dir / "AGENT.md").write_text("You are Pickle.\n")

            instruction = BehaviorLoader.load(behavior_dir)

        self.assertEqual("You are Pickle.", instruction)


if __name__ == "__main__":
    unittest.main()
