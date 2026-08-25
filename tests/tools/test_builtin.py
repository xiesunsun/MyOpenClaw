import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pickel.tools.base import ToolExecutionContext
from pickel.tools.bus import ToolActivation, ToolBus
from pickel.tools.catalog import builtin_tools, install_builtin_tools
from pickel.tools.file_service import WorkspaceFileService
from pickel.tools.policy import WorkspacePathAccessPolicy
from pickel.shared.execution_identity import ExecutionIdentity
from pickel.tools.services import ToolServices


class BuiltinToolTests(unittest.IsolatedAsyncioTestCase):
    def test_builtin_model_contracts_are_self_describing(self) -> None:
        specs = {tool.spec.name: tool.spec for tool in builtin_tools()}
        bus = ToolBus()
        install_builtin_tools(bus)
        snapshot = bus.snapshot(ToolActivation(allowed=frozenset(bus.list_names())))
        definitions = {entry.name: entry.tool.spec for entry in snapshot.entries}

        self.assertEqual(
            {"ls", "glob", "grep", "read", "edit", "write", "bash"},
            set(definitions),
        )
        self.assertIn("does not recurse", definitions["ls"].description)
        self.assertIn("respects Git ignore rules", definitions["glob"].description)
        self.assertIn("path:line:text", definitions["grep"].description)
        self.assertIn("UTF-8", definitions["read"].description)
        self.assertIn("without changing the file", definitions["edit"].description)
        self.assertIn("completely overwrite", definitions["write"].description)
        self.assertIn("non-zero exit code", definitions["bash"].description)

        expected_defaults = {
            "ls": {"path": ".", "limit": 500},
            "glob": {"path": ".", "limit": 1_000},
            "grep": {"path": ".", "ignore_case": False, "limit": 100},
            "read": {"offset": 1, "limit": 2_000},
        }
        for tool_name, defaults in expected_defaults.items():
            properties = definitions[tool_name].input_schema["properties"]
            self.assertEqual(
                defaults,
                {name: properties[name]["default"] for name in defaults},
            )

        self.assertEqual(
            ["ready", "running", "terminated"],
            specs["bash"].output_schema["properties"]["shell_status"]["enum"],
        )

    def test_builtin_tool_catalog_can_seed_bus(self) -> None:
        bus = ToolBus()
        install_builtin_tools(bus)

        tools = [
            bus.get(name).tool
            for name in [
                "ls",
                "glob",
                "grep",
                "read",
                "edit",
                "write",
                "bash",
            ]
        ]

        self.assertEqual(
            [
                "ls",
                "glob",
                "grep",
                "read",
                "edit",
                "write",
                "bash",
            ],
            [tool.spec.name for tool in tools],
        )

    async def test_builtin_read_tool_reads_relative_path_from_workspace(self) -> None:
        bus = ToolBus()
        install_builtin_tools(bus)
        read_tool = bus.get("read").tool

        with TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            (workspace / "note.txt").write_text("hello\nworld\n", encoding="utf-8")

            result = await read_tool.execute(
                {"path": "note.txt"},
                ToolExecutionContext(
                    agent_id="Pickle",
                    identity=ExecutionIdentity(session_id="session-1"),
                    workspace_path=workspace,
                    services=ToolServices(
                        workspace_files=WorkspaceFileService(
                            workspace_root=workspace,
                            access_policy=WorkspacePathAccessPolicy(),
                        )
                    ),
                ),
            )

        self.assertIn("1: hello", result.content)
        self.assertIn("2: world", result.content)
        self.assertFalse(result.is_error)


class _FakeActivationControl:
    def __init__(self, *, allowed: set[str], disabled: set[str] | None = None) -> None:
        self._allowed = frozenset(allowed)
        self.disabled = set(disabled or set())

    def allowed_names(self) -> frozenset[str]:
        return self._allowed

    def disable_tools(self, names) -> None:
        self.disabled |= set(names)

    def enable_tools(self, names) -> None:
        self.disabled -= set(names)


class ToolSetActiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_disable_narrows_activation_for_next_turn(self) -> None:
        from pickel.tools.builtin import tool_set_active

        control = _FakeActivationControl(allowed={"read_file", "bash"})
        context = ToolExecutionContext(
            agent_id="Pickle",
            identity=ExecutionIdentity(session_id="session-1"),
            workspace_path=Path("/tmp/pickle"),
            services=ToolServices(activation_control=control),
        )

        result = await tool_set_active.execute({"disable": ["bash"]}, context)

        self.assertFalse(result.is_error)
        self.assertEqual({"bash"}, control.disabled)
        self.assertIn("next turn", result.content.lower())

    async def test_enable_restores_previously_disabled_tool(self) -> None:
        from pickel.tools.builtin import tool_set_active

        control = _FakeActivationControl(allowed={"read_file"}, disabled={"read_file"})
        context = ToolExecutionContext(
            agent_id="Pickle",
            identity=ExecutionIdentity(session_id="session-1"),
            workspace_path=Path("/tmp/pickle"),
            services=ToolServices(activation_control=control),
        )

        result = await tool_set_active.execute({"enable": ["read_file"]}, context)

        self.assertFalse(result.is_error)
        self.assertEqual(set(), control.disabled)

    async def test_enabling_tool_outside_allowlist_is_an_error(self) -> None:
        from pickel.tools.builtin import tool_set_active

        control = _FakeActivationControl(allowed={"read_file"})
        context = ToolExecutionContext(
            agent_id="Pickle",
            identity=ExecutionIdentity(session_id="session-1"),
            workspace_path=Path("/tmp/pickle"),
            services=ToolServices(activation_control=control),
        )

        result = await tool_set_active.execute({"enable": ["bash"]}, context)

        self.assertTrue(result.is_error)
        self.assertIn("bash", result.content)
        self.assertEqual(set(), control.disabled)

    async def test_missing_activation_control_is_an_error(self) -> None:
        from pickel.tools.builtin import tool_set_active

        context = ToolExecutionContext(
            agent_id="Pickle",
            identity=ExecutionIdentity(session_id="session-1"),
            workspace_path=Path("/tmp/pickle"),
        )

        result = await tool_set_active.execute({"disable": ["read_file"]}, context)

        self.assertTrue(result.is_error)

    async def test_empty_request_is_an_error(self) -> None:
        from pickel.tools.builtin import tool_set_active

        control = _FakeActivationControl(allowed={"read_file"})
        context = ToolExecutionContext(
            agent_id="Pickle",
            identity=ExecutionIdentity(session_id="session-1"),
            workspace_path=Path("/tmp/pickle"),
            services=ToolServices(activation_control=control),
        )

        result = await tool_set_active.execute({}, context)

        self.assertTrue(result.is_error)


if __name__ == "__main__":
    unittest.main()
