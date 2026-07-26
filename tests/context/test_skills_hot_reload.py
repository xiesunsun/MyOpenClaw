"""skills_path 非空时，两次 prepare 之间目录变化会反映到 catalog。"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from pickel.agents.agent import Agent
from pickel.context.prepare import prepare
from pickel.conversations.session import Session
from pickel.shared.model_config import ModelConfig


def _write_skill(root: Path, name: str, description: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        textwrap.dedent(
            f"""\
            ---
            name: {name}
            description: {description}
            ---

            # {name}
            """
        ),
        encoding="utf-8",
    )


def test_prepare_rediscovers_skills_when_skills_path_set():
    with TemporaryDirectory() as tmpdir:
        skills_root = Path(tmpdir)
        _write_skill(skills_root, "alpha", "First skill.")

        agent = Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_config=ModelConfig(
                provider="google/gemini",
                model="gemini-3-flash-preview",
            ),
            tool_ids=[],
            skills=[],  # 故意不预填；catalog 应来自 discover
            skills_path=skills_root,
        )
        run = SimpleNamespace(agent=agent, tools=[], unit_window=5)
        session = Session.create(agent_id="Pickle")

        first = asyncio.run(prepare(run=run, session=session))
        assert "alpha: First skill." in first.system.as_text()
        assert "beta:" not in first.system.as_text()

        _write_skill(skills_root, "beta", "Second skill.")
        second = asyncio.run(prepare(run=run, session=session))
        assert "alpha: First skill." in second.system.as_text()
        assert "beta: Second skill." in second.system.as_text()


def test_prepare_uses_agent_skills_when_skills_path_is_none():
    """无 skills_path 时使用 agent.skills，不扫盘。"""
    with TemporaryDirectory() as tmpdir:
        skills_root = Path(tmpdir)
        _write_skill(skills_root, "disk-only", "On disk only.")

        agent = Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_config=ModelConfig(
                provider="google/gemini",
                model="gemini-3-flash-preview",
            ),
            tool_ids=[],
            skills=[],
            skills_path=None,
        )
        run = SimpleNamespace(agent=agent, tools=[], unit_window=5)
        session = Session.create(agent_id="Pickle")

        ctx = asyncio.run(prepare(run=run, session=session))
        assert "disk-only" not in ctx.system.as_text()
        assert ctx.system.as_text() == "You are Pickle."
