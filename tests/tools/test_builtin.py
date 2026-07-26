from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from pickel.tools.services import ToolServices
from pickel.tools.base import ToolExecutionContext
from pickel.tools.catalog import builtin_tools, install_builtin_tools
from pickel.tools.file_service import WorkspaceFileService
from pickel.tools.policy import WorkspacePathAccessPolicy
from pickel.tools.bus import ToolBus


class BuiltinToolTests(unittest.IsolatedAsyncioTestCase):
    def test_builtin_tool_catalog_can_seed_bus(self) -> None:
        bus = ToolBus()
        install_builtin_tools(bus)

        tools = [
            bus.get(name).tool
            for name in [
                "echo",
                "list_directory",
                "glob_search",
                "grep_search",
                "read_file",
                "read_many_files",
                "replace",
                "write_file",
                "shell_exec",
                "shell_restart",
                "shell_close",
            ]
        ]

        self.assertEqual(
            [
                "echo",
                "list_directory",
                "glob_search",
                "grep_search",
                "read_file",
                "read_many_files",
                "replace",
                "write_file",
                "shell_exec",
                "shell_restart",
                "shell_close",
            ],
            [tool.spec.name for tool in tools],
        )

    async def test_builtin_read_tool_reads_relative_path_from_workspace(self) -> None:
        bus = ToolBus()
        install_builtin_tools(bus)
        read_tool = bus.get("read_file").tool

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

        self.assertIn("File: note.txt", result.content)
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

        control = _FakeActivationControl(allowed={"read_file", "shell_exec"})
        context = ToolExecutionContext(
            agent_id="Pickle",
            session_id="session-1",
            workspace_path=Path("/tmp/pickle"),
            services=ToolServices(activation_control=control),
        )

        result = await tool_set_active.execute({"disable": ["shell_exec"]}, context)

        self.assertFalse(result.is_error)
        self.assertEqual({"shell_exec"}, control.disabled)
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

        result = await tool_set_active.execute({"enable": ["shell_exec"]}, context)

        self.assertTrue(result.is_error)
        self.assertIn("shell_exec", result.content)
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
