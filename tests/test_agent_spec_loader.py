import tempfile
import textwrap
import unittest
from pathlib import Path
import sys

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from myopenclaw.agent.behavior_loader import BehaviorLoader


class BehaviorLoaderTests(unittest.TestCase):
    def test_load_reads_instruction_from_agent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            behavior_dir = Path(tmpdir) / "agents" / "default"
            behavior_dir.mkdir(parents=True)
            (behavior_dir / "AGENT.md").write_text(
                textwrap.dedent(
                    """
                    ---
                    name: Default Agent
                    ---

                    You are the default agent.
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            instruction = BehaviorLoader.load(behavior_dir)

        self.assertEqual(instruction, "You are the default agent.")

    def test_load_strips_frontmatter_when_behavior_path_is_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            behavior_file = Path(tmpdir) / "AGENT.md"
            behavior_file.write_text(
                textwrap.dedent(
                    """
                    ---
                    name: Default Agent
                    description: test agent
                    ---

                    You are the default agent.
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            instruction = BehaviorLoader.load(behavior_file)

        self.assertEqual(instruction, "You are the default agent.")
