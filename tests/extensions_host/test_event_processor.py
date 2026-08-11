"""会话级事件处理器注册与求值。"""

import unittest

from pickel.extensions_host.event_processor import ConversationExtensionContext
from pickel.extensions_host.host import ExtensionHost
from pickel.extensions_host.registry import ExtensionRegistry
from pickel.runtime.runtime_events import AssistantMessageEvent
from pickel.tools.bus import ToolBus


class _Processor:
    async def handle_event(self, event) -> None:
        pass

    def close(self) -> None:
        pass


def _context(mode="interactive"):
    async def publish(output) -> None:
        pass

    def start(coroutine, name) -> None:
        coroutine.close()

    return ConversationExtensionContext(
        agent_id="Pickle",
        session_id="session-1",
        mode=mode,
        publish_output=publish,
        start_background_task=start,
    )


class EventProcessorRegistryTests(unittest.TestCase):
    def test_resolves_processor_with_declared_events(self) -> None:
        registry = ExtensionRegistry()
        host = ExtensionHost(
            name="demo",
            config_section=None,
            tool_bus=ToolBus(),
            registry=registry,
        )
        processor = _Processor()
        host.add_event_processor(
            event_types=(AssistantMessageEvent,),
            factory=lambda context: (
                processor if context.mode == "interactive" else None
            ),
        )

        resolved = registry.resolve_event_processors(_context())

        self.assertEqual(1, len(resolved))
        self.assertIs(processor, resolved[0].processor)
        self.assertEqual((AssistantMessageEvent,), resolved[0].event_types)

    def test_factory_can_disable_batch_mode(self) -> None:
        registry = ExtensionRegistry()
        host = ExtensionHost(
            name="demo",
            config_section=None,
            tool_bus=ToolBus(),
            registry=registry,
        )
        host.add_event_processor(
            event_types=(AssistantMessageEvent,),
            factory=lambda context: (
                _Processor() if context.mode == "interactive" else None
            ),
        )

        resolved = registry.resolve_event_processors(_context("batch"))

        self.assertEqual([], resolved)

    def test_rejects_empty_event_types(self) -> None:
        host = ExtensionHost(
            name="demo",
            config_section=None,
            tool_bus=ToolBus(),
            registry=ExtensionRegistry(),
        )

        with self.assertRaisesRegex(ValueError, "event_types"):
            host.add_event_processor(
                event_types=(),
                factory=lambda context: _Processor(),
            )


if __name__ == "__main__":
    unittest.main()
