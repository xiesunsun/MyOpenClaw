import unittest

from myopenclaw.infrastructure.tools.catalog import builtin_tools
from myopenclaw.infrastructure.tools.registry import ToolRegistry


class ToolRegistryTests(unittest.TestCase):
    def test_registry_resolves_builtin_tools_from_catalog(self) -> None:
        registry = ToolRegistry(tools=builtin_tools())

        tools = registry.resolve_many(["echo", "read_file"])

        self.assertEqual(["echo", "read_file"], [tool.spec.name for tool in tools])

    def test_unknown_tool_id_raises_key_error(self) -> None:
        registry = ToolRegistry(tools=builtin_tools())

        with self.assertRaises(KeyError):
            registry.resolve_many(["missing-tool"])


if __name__ == "__main__":
    unittest.main()
