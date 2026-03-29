import importlib
import unittest


class RuntimeProtocolsRemovalTests(unittest.TestCase):
    def test_runtime_protocols_generation_module_is_not_importable(self) -> None:
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("myopenclaw.runtime_protocols.generation")


if __name__ == "__main__":
    unittest.main()
