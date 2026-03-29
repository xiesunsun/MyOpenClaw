from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from myopenclaw.tools.base import ToolExecutionContext
from myopenclaw.tools.builtin import read_file
from myopenclaw.tools.catalog import builtin_tools
from myopenclaw.tools.registry import ToolRegistry


class BuiltinToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_file_returns_file_contents(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "note.txt"
            path.write_text("hello")

            result = await read_file.execute(
                {"path": str(path)},
                ToolExecutionContext(
                    agent_id="Pickle",
                    session_id="session-1",
                    workspace_path=Path(tmpdir),
                ),
            )

        self.assertEqual("hello", result.content)
        self.assertFalse(result.is_error)

    async def test_read_file_returns_error_message_for_missing_file(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "missing.txt"

            result = await read_file.execute(
                {"path": str(path)},
                ToolExecutionContext(
                    agent_id="Pickle",
                    session_id="session-1",
                    workspace_path=Path(tmpdir),
                ),
            )

        self.assertEqual(f"Error: File not found: {path}", result.content)

    def test_builtin_tool_catalog_can_seed_registry(self) -> None:
        registry = ToolRegistry(tools=builtin_tools())

        tool = registry.resolve("read")

        self.assertEqual("read", tool.spec.name)


if __name__ == "__main__":
    unittest.main()
