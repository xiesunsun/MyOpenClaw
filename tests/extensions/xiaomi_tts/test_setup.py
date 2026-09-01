"""小米自动语音 extension 装配。"""

import unittest
from unittest.mock import patch

from pickel.extensions.xiaomi_tts import setup
from pickel.extensions_host.event_processor import ConversationExtensionContext
from pickel.extensions_host.host import ExtensionHost
from pickel.extensions_host.registry import ExtensionRegistry
from pickel.tools.bus import ToolBus


def _host(section=None):
    registry = ExtensionRegistry()
    return (
        ExtensionHost(
            name="xiaomi_tts",
            config_section=section,
            tool_bus=ToolBus(),
            registry=registry,
        ),
        registry,
    )


class XiaomiTtsSetupTests(unittest.TestCase):
    def test_explicit_config_enables_interactive_processor(self) -> None:
        host, registry = _host({"enabled": True})

        with patch.dict("os.environ", {"XIAOMI_API_KEY": "secret"}, clear=True):
            setup(host)

        async def publish(output) -> None:
            pass

        def start(coroutine, name) -> None:
            coroutine.close()

        interactive = registry.resolve_event_processors(
            ConversationExtensionContext(
                "Pickle", "session-1", "interactive", publish, start
            )
        )
        batch = registry.resolve_event_processors(
            ConversationExtensionContext("Pickle", "session-1", "batch", publish, start)
        )

        self.assertEqual(1, len(interactive))
        self.assertEqual([], batch)
        interactive[0].processor.close()

    def test_missing_key_keeps_unconfigured_extension_disabled(self) -> None:
        host, registry = _host()

        with patch.dict("os.environ", {}, clear=True):
            setup(host)

        self.assertEqual([], registry.event_processors)

    def test_environment_key_does_not_implicitly_enable_extension(self) -> None:
        host, registry = _host()

        with patch.dict("os.environ", {"XIAOMI_API_KEY": "secret"}, clear=True):
            setup(host)

        self.assertEqual([], registry.event_processors)

    def test_explicit_disable_does_not_require_key(self) -> None:
        host, registry = _host({"enabled": False})

        with patch.dict("os.environ", {}, clear=True):
            setup(host)

        self.assertEqual([], registry.event_processors)

    def test_explicit_enable_requires_key(self) -> None:
        host, _ = _host({"enabled": True})

        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "XIAOMI_API_KEY"):
                setup(host)


if __name__ == "__main__":
    unittest.main()
