import unittest

from pickel.tools.base import BaseTool, ToolSpec
from pickel.tools.bus import (
    ToolActivation,
    ToolBus,
    ToolNameConflictError,
    ToolSource,
)


def _stub_tool(name: str) -> BaseTool:
    """造一个只有 spec 的最小工具，够 bus 测试用。"""

    class _Stub(BaseTool):
        spec = ToolSpec(
            name=name,
            description=f"{name} description",
            input_schema={"type": "object", "properties": {}},
            output_schema={"type": "string"},
        )

    return _Stub()


class ToolBusRegistrationTests(unittest.TestCase):
    def test_builtin_tool_keeps_bare_name(self) -> None:
        bus = ToolBus()

        name = bus.register(_stub_tool("read_file"), source=ToolSource.BUILTIN)

        self.assertEqual("read_file", name)
        self.assertEqual(["read_file"], bus.list_names())

    def test_mcp_tool_gets_namespace_prefix_but_spec_stays_bare(self) -> None:
        bus = ToolBus()

        name = bus.register(
            _stub_tool("create_issue"),
            source=ToolSource.MCP,
            origin="github",
        )

        self.assertEqual("mcp__github__create_issue", name)
        entry = bus.get("mcp__github__create_issue")
        self.assertEqual("mcp__github__create_issue", entry.name)
        self.assertEqual("create_issue", entry.tool.spec.name)

    def test_extension_tool_uses_its_own_prefix(self) -> None:
        bus = ToolBus()

        name = bus.register(
            _stub_tool("recall_search"),
            source=ToolSource.EXTENSION,
            origin="openviking",
        )

        # extension 工具跑在本进程内，与 MCP 的子进程工具前缀必须分开
        self.assertEqual("ext__openviking__recall_search", name)
        self.assertEqual(ToolSource.EXTENSION, bus.get(name).source)

    def test_same_origin_across_mcp_and_extension_does_not_collide(self) -> None:
        bus = ToolBus()

        mcp_name = bus.register(
            _stub_tool("run"), source=ToolSource.MCP, origin="shared"
        )
        ext_name = bus.register(
            _stub_tool("run"), source=ToolSource.EXTENSION, origin="shared"
        )

        self.assertEqual("mcp__shared__run", mcp_name)
        self.assertEqual("ext__shared__run", ext_name)
        self.assertEqual(2, len(bus.list_names()))

    def test_non_builtin_without_origin_is_rejected(self) -> None:
        bus = ToolBus()

        with self.assertRaises(ValueError):
            bus.register(_stub_tool("create_issue"), source=ToolSource.MCP)

    def test_same_source_and_origin_overwrites_and_keeps_enabled_flag(self) -> None:
        bus = ToolBus()
        name = bus.register(
            _stub_tool("create_issue"), source=ToolSource.MCP, origin="github"
        )
        bus.set_enabled(name, False)

        replacement = _stub_tool("create_issue")
        bus.register(replacement, source=ToolSource.MCP, origin="github", version="v2")

        entry = bus.get(name)
        self.assertIs(replacement, entry.tool)
        self.assertEqual("v2", entry.version)
        self.assertFalse(entry.enabled)  # 运维关掉的工具不因重连自动打开

    def test_origin_containing_double_underscore_is_rejected(self) -> None:
        # 否则 server "a" + 工具 "b__c" 与 server "a__b" + 工具 "c"
        # 会得到同一个 mcp__a__b__c，前缀方案出现歧义
        bus = ToolBus()

        with self.assertRaises(ValueError):
            bus.register(_stub_tool("c"), source=ToolSource.MCP, origin="a__b")

    def test_builtin_name_shaped_like_a_qualified_name_conflicts(self) -> None:
        # 唯一还能触发跨来源撞名的场景：内置工具名恰好长成前缀形式
        bus = ToolBus()
        bus.register(_stub_tool("mcp__github__create_issue"), source=ToolSource.BUILTIN)

        with self.assertRaises(ToolNameConflictError):
            bus.register(
                _stub_tool("create_issue"), source=ToolSource.MCP, origin="github"
            )

    def test_builtin_and_prefixed_tool_never_collide(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("read_file"), source=ToolSource.BUILTIN)
        bus.register(_stub_tool("read_file"), source=ToolSource.MCP, origin="remote")

        self.assertEqual(
            ["mcp__remote__read_file", "read_file"],
            sorted(bus.list_names()),
        )

    def test_unknown_name_raises_key_error(self) -> None:
        bus = ToolBus()

        with self.assertRaises(KeyError):
            bus.get("missing_tool")


class ToolBusLifecycleTests(unittest.TestCase):
    def test_registration_lease_does_not_remove_later_same_name_entry(self) -> None:
        bus = ToolBus()
        first = bus.register_lease(
            _stub_tool("echo"), source=ToolSource.MCP, origin="github"
        )
        replacement = _stub_tool("echo")
        bus.register(replacement, source=ToolSource.MCP, origin="github")

        import asyncio

        asyncio.run(first.close())

        self.assertIs(replacement, bus.get("mcp__github__echo").tool)
        asyncio.run(first.close())
        self.assertEqual(["mcp__github__echo"], bus.list_names())

    def test_unregister_removes_single_entry(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("echo"), source=ToolSource.BUILTIN)

        bus.unregister("echo")

        self.assertEqual([], bus.list_names())

    def test_unregister_origin_removes_all_of_that_server(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("echo"), source=ToolSource.BUILTIN)
        bus.register(_stub_tool("a"), source=ToolSource.MCP, origin="github")
        bus.register(_stub_tool("b"), source=ToolSource.MCP, origin="github")
        bus.register(_stub_tool("c"), source=ToolSource.MCP, origin="slack")

        removed = bus.unregister_origin(ToolSource.MCP, "github")

        self.assertEqual(["mcp__github__a", "mcp__github__b"], sorted(removed))
        self.assertEqual(["echo", "mcp__slack__c"], sorted(bus.list_names()))

    def test_list_filters_by_source(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("echo"), source=ToolSource.BUILTIN)
        bus.register(_stub_tool("a"), source=ToolSource.MCP, origin="github")

        self.assertEqual(["echo"], bus.list_names(source=ToolSource.BUILTIN))
        self.assertEqual(["mcp__github__a"], bus.list_names(source=ToolSource.MCP))

    def test_set_enabled_toggles_without_removing(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("echo"), source=ToolSource.BUILTIN)

        bus.set_enabled("echo", False)

        self.assertFalse(bus.get("echo").enabled)
        self.assertEqual(["echo"], bus.list_names())

    def test_set_enabled_on_unknown_name_raises(self) -> None:
        bus = ToolBus()

        with self.assertRaises(KeyError):
            bus.set_enabled("missing_tool", False)


class ToolActivationTests(unittest.TestCase):
    def test_snapshot_intersects_allowlist_and_enabled(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("read_file"), source=ToolSource.BUILTIN)
        bus.register(_stub_tool("write_file"), source=ToolSource.BUILTIN)
        bus.register(_stub_tool("shell_exec"), source=ToolSource.BUILTIN)
        bus.register(_stub_tool("echo"), source=ToolSource.BUILTIN)
        bus.set_enabled("write_file", False)

        activation = ToolActivation(
            allowed=frozenset({"read_file", "write_file", "shell_exec"}),
        )
        snapshot = bus.snapshot(activation)

        # write_file 被 bus 禁用、echo 不在白名单
        self.assertEqual(("read_file", "shell_exec"), snapshot.names)

    def test_allowlist_entry_missing_from_bus_is_skipped_not_raised(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("read_file"), source=ToolSource.BUILTIN)

        activation = ToolActivation(
            allowed=frozenset({"read_file", "mcp__github__create_issue"})
        )

        self.assertEqual(("read_file",), bus.snapshot(activation).names)
        self.assertEqual(["mcp__github__create_issue"], bus.missing_names(activation))


class ToolSnapshotTests(unittest.TestCase):
    def test_snapshot_is_immune_to_later_bus_changes(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("read_file"), source=ToolSource.BUILTIN)
        activation = ToolActivation(allowed=frozenset({"read_file", "write_file"}))
        snapshot = bus.snapshot(activation)

        bus.register(_stub_tool("write_file"), source=ToolSource.BUILTIN)
        bus.set_enabled("read_file", False)
        bus.unregister("read_file")

        # 快照在 turn 内不可变：既看不到新注册的，也不受禁用与卸载影响
        self.assertEqual(("read_file",), snapshot.names)
        self.assertIsNotNone(snapshot.find("read_file"))

    def test_find_returns_none_for_unknown_name(self) -> None:
        bus = ToolBus()
        snapshot = bus.snapshot(ToolActivation(allowed=frozenset()))

        self.assertIsNone(snapshot.find("read_file"))

    def test_find_uses_entry_name_not_spec_name(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("create_issue"), source=ToolSource.MCP, origin="github")
        activation = ToolActivation(allowed=frozenset({"mcp__github__create_issue"}))

        snapshot = bus.snapshot(activation)

        self.assertIsNotNone(snapshot.find("mcp__github__create_issue"))
        self.assertIsNone(snapshot.find("create_issue"))


if __name__ == "__main__":
    unittest.main()


class WildcardActivationTests(unittest.TestCase):
    def _bus_with_mcp_tools(self) -> ToolBus:
        bus = ToolBus()
        for tool_name in ("create_issue", "list_repos"):
            bus.register(_stub_tool(tool_name), source=ToolSource.MCP, origin="github")
        return bus

    def test_snapshot_matches_wildcard_pattern(self) -> None:
        bus = self._bus_with_mcp_tools()

        snapshot = bus.snapshot(ToolActivation(allowed=frozenset({"mcp__github__*"})))

        self.assertEqual(
            {"mcp__github__create_issue", "mcp__github__list_repos"},
            set(snapshot.names),
        )

    def test_snapshot_exact_names_still_work(self) -> None:
        bus = self._bus_with_mcp_tools()

        snapshot = bus.snapshot(
            ToolActivation(allowed=frozenset({"mcp__github__create_issue"}))
        )

        self.assertEqual(("mcp__github__create_issue",), snapshot.names)

    def test_missing_names_wildcard_only_when_nothing_matches(self) -> None:
        bus = self._bus_with_mcp_tools()

        activation = ToolActivation(
            allowed=frozenset({"mcp__github__*", "mcp__jira__*", "read_file"})
        )

        self.assertEqual(["mcp__jira__*", "read_file"], bus.missing_names(activation))
