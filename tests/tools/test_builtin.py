import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pickel.tools.base import ToolExecutionContext
from pickel.tools.bus import ToolBus
from pickel.tools.catalog import install_builtin_tools
from pickel.tools.file_service import WorkspaceFileService
from pickel.tools.policy import WorkspacePathAccessPolicy
from pickel.tools.services import ToolServices


class BuiltinToolTests(unittest.IsolatedAsyncioTestCase):
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
                    session_id="session-1",
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
            session_id="session-1",
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
            session_id="session-1",
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
            session_id="session-1",
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
            session_id="session-1",
            workspace_path=Path("/tmp/pickle"),
        )

        result = await tool_set_active.execute({"disable": ["read_file"]}, context)

        self.assertTrue(result.is_error)

    async def test_empty_request_is_an_error(self) -> None:
        from pickel.tools.builtin import tool_set_active

        control = _FakeActivationControl(allowed={"read_file"})
        context = ToolExecutionContext(
            agent_id="Pickle",
            session_id="session-1",
            workspace_path=Path("/tmp/pickle"),
            services=ToolServices(activation_control=control),
        )

        result = await tool_set_active.execute({}, context)

        self.assertTrue(result.is_error)


if __name__ == "__main__":
    unittest.main()
