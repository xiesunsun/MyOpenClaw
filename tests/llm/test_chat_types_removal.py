import importlib
import unittest


class ChatTypesRemovalTests(unittest.TestCase):
    def test_llm_chat_types_module_is_not_importable(self) -> None:
        with self.assertRaises(ModuleNotFoundError):
            importlib.import_module("myopenclaw.llm.chat_types")


if __name__ == "__main__":
    unittest.main()
