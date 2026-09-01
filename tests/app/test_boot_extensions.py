"""Boot 从 ExtensionRegistry 取贡献，并按 agent 求值。"""

import unittest
from types import SimpleNamespace

from pickel.app.boot import Boot
from pickel.extensions_host.registry import ExtensionRegistry


class BootExtensionWiringTests(unittest.TestCase):
    def test_registry_defaults_to_empty_when_not_injected(self) -> None:
        boot = Boot.from_config(SimpleNamespace(extensions={}))

        self.assertEqual([], boot.extensions.extension_names)

    def test_recall_factories_are_evaluated_per_agent(self) -> None:
        registry = ExtensionRegistry()
        seen: list[str] = []

        def _factory(scope):
            seen.append(scope.agent_id)
            return f"recall-{scope.agent_id}"

        registry.recall_factories.append(_factory)
        boot = Boot.from_config(SimpleNamespace(extensions={}), extensions=registry)

        sources = boot.resolve_recall_sources("Pickle")

        self.assertEqual(["recall-Pickle"], sources)
        self.assertEqual(["Pickle"], seen)

    def test_hook_handlers_come_from_registry(self) -> None:
        registry = ExtensionRegistry()
        registry.hook_factories.append(lambda scope: "handler-a")
        registry.hook_factories.append(lambda scope: None)
        boot = Boot.from_config(SimpleNamespace(extensions={}), extensions=registry)

        self.assertEqual(["handler-a"], boot.resolve_hook_handlers("Pickle"))


if __name__ == "__main__":
    unittest.main()
