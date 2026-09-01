import unittest
from pathlib import Path

from pickel.shared.execution_identity import ExecutionIdentity
from pickel.tools.base import ToolExecutionContext, ToolExecutionError
from pickel.tools.update_plan import update_plan
from pickel.tools.validation import validate_tool_output


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        agent_id="Pickle",
        identity=ExecutionIdentity(session_id="session-1"),
        workspace_path=Path("."),
    )


class UpdatePlanToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_compact_counts_and_is_safe(self) -> None:
        result = await update_plan.execute(
            {
                "explanation": "拆分任务",
                "plan": [
                    {"step": "分析现有实现", "status": "completed"},
                    {"step": "实现工具", "status": "in_progress"},
                    {"step": "补测试", "status": "pending"},
                ],
            },
            _context(),
        )

        self.assertEqual(
            result,
            {
                "updated": True,
                "active": True,
                "item_count": 3,
                "completed_count": 1,
            },
        )
        self.assertEqual(update_plan.spec.replay_policy, "safe")
        self.assertIsNone(validate_tool_output(update_plan, result))
        self.assertIsNotNone(validate_tool_output(update_plan, {"updated": True}))
        self.assertEqual(
            update_plan.render(result)[0].text,
            '{"active":true,"completed_count":1,"item_count":3,"updated":true}',
        )

    async def test_all_completed_is_inactive(self) -> None:
        result = await update_plan.execute(
            {"plan": [{"step": "完成", "status": "completed"}]}, _context()
        )

        self.assertFalse(result["active"])
        self.assertEqual(result["completed_count"], 1)

    async def test_rejects_invalid_plan(self) -> None:
        invalid_plans = [
            [],
            [{"step": "   ", "status": "pending"}],
            [{"step": "x" * 501, "status": "pending"}],
            [
                {"step": "a", "status": "in_progress"},
                {"step": "b", "status": "in_progress"},
            ],
        ]
        for plan in invalid_plans:
            with self.subTest(plan=plan):
                with self.assertRaises(ToolExecutionError):
                    await update_plan.execute({"plan": plan}, _context())
