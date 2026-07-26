"""turn 边界快照语义：turn 内改 bus 不影响本 turn 的工具集。"""

import unittest

from pickel.runs.turn_state import TurnState
from pickel.tools.base import BaseTool, ToolSpec
from pickel.tools.bus import ToolActivation, ToolBus, ToolSource


def _stub_tool(name: str) -> BaseTool:
    class _Stub(BaseTool):
        spec = ToolSpec(
            name=name,
            description=f"{name} description",
            input_schema={"type": "object", "properties": {}},
        )

    return _Stub()


class TurnStateSnapshotTests(unittest.TestCase):
    def test_turn_state_holds_no_snapshot_by_default(self) -> None:
        self.assertIsNone(TurnState().tool_snapshot)

    def test_snapshot_stays_stable_after_bus_mutation_within_turn(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("read_file"), source=ToolSource.BUILTIN)
        activation = ToolActivation(allowed=frozenset({"read_file", "write_file"}))

        turn = TurnState()
        turn.tool_snapshot = bus.snapshot(activation)

        # 模拟 turn 中间发生的热插拔
        bus.register(_stub_tool("write_file"), source=ToolSource.BUILTIN)
        bus.set_enabled("read_file", False)

        self.assertEqual(("read_file",), turn.tool_snapshot.names)
        self.assertIsNotNone(turn.tool_snapshot.find("read_file"))
        self.assertIsNone(turn.tool_snapshot.find("write_file"))


if __name__ == "__main__":
    unittest.main()
