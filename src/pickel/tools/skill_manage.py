"""skill_manage：agent 的 skill 写入通道。写入默认进待审队列。"""

from __future__ import annotations

import asyncio
from typing import Any

from pickel.skills.guard import SkillGuardError
from pickel.skills.store import SkillStoreError, SkillWriteRequest
from pickel.tools.base import (
    BaseTool,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolSpec,
)


class SkillManageTool(BaseTool):
    spec = ToolSpec(
        name="skill_manage",
        description=(
            "Create, patch or delete a skill in this agent's skills directory. "
            "Writes are staged for the user's approval by default — the change only "
            "takes effect after they approve it, and it becomes available on the next turn. "
            "Prefer 'patch' over rewriting a whole skill: it replaces one exact snippet "
            "and fails loudly if the snippet is not unique."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "patch", "delete"],
                    "description": "What to do with the skill.",
                },
                "skill_name": {
                    "type": "string",
                    "description": (
                        "Skill directory name: lowercase letters, digits, hyphens."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": (
                        "create: the full SKILL.md content including frontmatter."
                    ),
                },
                "old_text": {
                    "type": "string",
                    "description": (
                        "patch: the exact snippet to replace (must be unique)."
                    ),
                },
                "new_text": {
                    "type": "string",
                    "description": "patch: what to replace it with.",
                },
            },
            "required": ["action", "skill_name"],
        },
    )

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        store = context.services.skill_store
        if store is None:
            return ToolExecutionResult(
                content=(
                    "Skill management is unavailable: this agent has no skills "
                    "directory configured."
                ),
                is_error=True,
            )
        request = SkillWriteRequest(
            action=str(arguments["action"]),
            skill_name=str(arguments["skill_name"]),
            content=str(arguments.get("content", "")),
            old_text=str(arguments.get("old_text", "")),
            new_text=str(arguments.get("new_text", "")),
        )
        try:
            outcome = await asyncio.to_thread(store.submit, request)
        except SkillGuardError as exc:
            return ToolExecutionResult(
                content=str(exc),
                is_error=True,
                metadata={"guard_rule": exc.rule, "action": request.action},
            )
        except SkillStoreError as exc:
            return ToolExecutionResult(
                content=str(exc),
                is_error=True,
                metadata={"action": request.action},
            )
        return ToolExecutionResult(
            content=outcome.message,
            metadata={
                "action": request.action,
                "skill_name": request.skill_name,
                "applied": outcome.applied,
                "pending_id": outcome.pending_id,
                "path": str(outcome.path) if outcome.path else None,
            },
        )
