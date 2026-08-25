"""Extension setup 的 draft 发布和生命周期隔离。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from pickel.extensions_host.loader import load_extensions, teardown_extensions
from pickel.extensions_host.registry import (
    AgentScope,
    ContributionScope,
    ExtensionRegistry,
)
from pickel.extensions_host.host import ExtensionHost
from pickel.runtime.runtime_events import AssistantMessageEvent
from pickel.tools.base import BaseTool, ToolSpec
from pickel.tools.bus import ToolBus, ToolSource


def _app_config(*, marker: Path | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        extensions={},
        marker=str(marker) if marker is not None else None,
    )


def _write_extension(home: Path, name: str, body: str) -> None:
    folder = home / "extensions" / name
    folder.mkdir(parents=True)
    (folder / "__init__.py").write_text(body, encoding="utf-8")


_ALL_CONTRIBUTIONS = """
from pickel.tools.base import BaseTool, ToolSpec
from pickel.runtime.runtime_events import AssistantMessageEvent

class Probe(BaseTool):
    spec = ToolSpec(name='probe', description='probe', input_schema={'type': 'object'})

class Processor:
    async def handle_event(self, event): pass
    def close(self): pass

def setup(host):
    host.register_tool(Probe())
    host.add_hook_handler(lambda scope: 'hook')
    host.add_recall_source(lambda scope: 'recall')
    host.add_event_processor(event_types=(AssistantMessageEvent,), factory=lambda context: Processor())
    host.add_provider('fake', lambda scope: 'provider')
    host.add_disposer(lambda: None)
"""


class ContributionScopeTests(unittest.TestCase):
    def test_scope_closes_leases_and_children_in_lifo_order(self) -> None:
        events: list[str] = []
        scope = ContributionScope("root")

        scope.add_disposer(lambda: events.append("root-first"))
        child = scope.child("child")
        child.add_disposer(lambda: events.append("child"))
        scope.add_disposer(lambda: events.append("root-last"))

        import asyncio

        asyncio.run(scope.close())
        asyncio.run(scope.close())

        self.assertEqual(["root-last", "child", "root-first"], events)

    def test_scope_reports_cleanup_failures_after_running_remaining_leases(
        self,
    ) -> None:
        events: list[str] = []
        scope = ContributionScope()

        def failing() -> None:
            events.append("failing")
            raise RuntimeError("expected cleanup failure")

        scope.add_disposer(lambda: events.append("first"))
        scope.add_disposer(failing)
        scope.add_disposer(lambda: events.append("last"))

        import asyncio

        asyncio.run(scope.close())

        self.assertEqual(["last", "failing", "first"], events)

    def test_success_publishes_all_contribution_types_once(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_extension(home, "all", _ALL_CONTRIBUTIONS)
            bus = ToolBus()

            result = load_extensions(
                tool_bus=bus,
                app_config=_app_config(),
                home=home,
                builtin_package=None,
            )

            self.assertEqual([], result.errors)
            self.assertEqual(["all"], result.registry.extension_names)
            self.assertEqual(
                ["ext__all__probe"], bus.list_names(source=ToolSource.EXTENSION)
            )
            scope = AgentScope(agent_id="Pickle", app_config=None)
            self.assertEqual(["hook"], result.registry.hook_handlers(scope))
            self.assertEqual(["recall"], result.registry.recall_sources(scope))
            self.assertEqual({"fake": "provider"}, result.registry.providers(scope))
            self.assertEqual(1, len(result.registry.event_processors))

    def test_provider_instance_is_kept_as_a_process_contribution(self) -> None:
        registry = ExtensionRegistry()
        provider = object()
        host = ExtensionHost(
            name="provider_ext",
            config_section=None,
            tool_bus=ToolBus(),
            registry=registry,
        )

        host.add_provider("fake", provider)

        self.assertIs(provider, registry.providers(AgentScope("Pickle", None))["fake"])

    def test_failure_discards_draft_and_runs_all_disposers_in_lifo_order(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            marker = Path(tmp) / "cleanup.log"
            _write_extension(
                home,
                "broken",
                """
from pathlib import Path
from pickel.tools.base import BaseTool, ToolSpec
class Probe(BaseTool):
    spec = ToolSpec(name='probe', description='probe', input_schema={'type': 'object'})
def setup(host):
    def mark(label, fail=False):
        def dispose():
            with Path(host.app_config.marker).open('a', encoding='utf-8') as file:
                file.write(label + '\\n')
            if fail:
                raise RuntimeError(label)
        return dispose
    host.register_tool(Probe())
    host.add_hook_handler(lambda scope: 'hook')
    host.add_recall_source(lambda scope: 'recall')
    host.add_provider('fake', lambda scope: 'provider')
    host.add_disposer(mark('first'))
    host.add_disposer(mark('second', fail=True))
    host.add_disposer(mark('third'))
    raise RuntimeError('setup failed')
""",
            )

            bus = ToolBus()
            result = load_extensions(
                tool_bus=bus,
                app_config=_app_config(marker=marker),
                home=home,
                builtin_package=None,
            )

            self.assertEqual(1, len(result.errors))
            self.assertEqual(
                ["third", "second", "first"], marker.read_text().splitlines()
            )
            self.assertEqual([], bus.list_names(source=ToolSource.EXTENSION))
            self.assertEqual([], result.registry.extension_names)
            self.assertEqual([], result.registry.hook_factories)
            self.assertEqual([], result.registry.recall_factories)
            self.assertEqual({}, result.registry.provider_factories)

    def test_successful_teardown_runs_disposers_in_lifo_order(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            marker = Path(tmp) / "cleanup.log"
            _write_extension(
                home,
                "healthy",
                """
from pathlib import Path
def setup(host):
    def mark(label):
        def dispose():
            with Path(host.app_config.marker).open('a', encoding='utf-8') as file:
                file.write(label + '\\n')
        return dispose
    host.add_disposer(mark('last'))
    host.add_disposer(mark('first'))
""",
            )
            bus = ToolBus()
            result = load_extensions(
                tool_bus=bus,
                app_config=_app_config(marker=marker),
                home=home,
                builtin_package=None,
            )

            import asyncio

            asyncio.run(teardown_extensions(result, tool_bus=bus))
            self.assertEqual(["first", "last"], marker.read_text().splitlines())

    def test_old_scope_close_does_not_remove_same_name_replacement(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_extension(
                home,
                "healthy",
                """
from pickel.tools.base import BaseTool, ToolSpec
class Probe(BaseTool):
    spec = ToolSpec(name='probe', description='probe', input_schema={'type': 'object'})
def setup(host):
    host.register_tool(Probe())
""",
            )
            bus = ToolBus()
            result = load_extensions(
                tool_bus=bus,
                app_config=_app_config(),
                home=home,
                builtin_package=None,
            )

            from pickel.tools.base import BaseTool, ToolSpec

            class NewProbe(BaseTool):
                spec = ToolSpec(
                    name="probe",
                    description="replacement",
                    input_schema={"type": "object"},
                )

            bus.register(NewProbe(), source=ToolSource.EXTENSION, origin="healthy")
            import asyncio

            asyncio.run(teardown_extensions(result, tool_bus=bus))

            self.assertEqual(["ext__healthy__probe"], bus.list_names())


class DraftStatusSourceTests(unittest.TestCase):
    def test_status_source_is_not_published_until_host_publish(self) -> None:
        registry = ExtensionRegistry()
        source = SimpleNamespace(snapshot=lambda: None)
        host = ExtensionHost(
            name="mcp",
            config_section=None,
            tool_bus=ToolBus(),
            registry=registry,
            defer_publish=True,
        )

        host.register_mcp_status_source(source)
        self.assertIsNone(registry.mcp_status_source)
        host.publish()
        self.assertIs(source, registry.mcp_status_source)


if __name__ == "__main__":
    unittest.main()
