from pathlib import Path
from tempfile import TemporaryDirectory
from io import StringIO
from types import SimpleNamespace
import unittest

from rich.console import Console

from pickel.cli.chat import ChatLoop
from pickel.app.runtime import RuntimeConversation
from pickel.skills.store import SkillStore, SkillWriteRequest

_BODY = "---\nname: demo\ndescription: Demo skill.\n---\n\n# Demo\n"


class SkillsCommandTests(unittest.TestCase):
    def _store(self, tmp: Path) -> SkillStore:
        return SkillStore(
            skills_path=tmp / "skills", pending_dir=tmp / "pending", agent_id="Pickle"
        )

    def _loop(self, store: SkillStore | None) -> ChatLoop:
        # 只测命令分发与输出：_handle_skills_command 只依赖 console 与 run.skill_store
        loop = ChatLoop.__new__(ChatLoop)
        self._output = StringIO()
        loop.console = Console(file=self._output, width=200, no_color=True)
        conversation = RuntimeConversation.__new__(RuntimeConversation)
        conversation._run = SimpleNamespace(skill_store=store)
        loop._conversation = conversation
        return loop

    def _printed(self, loop: ChatLoop) -> str:
        return self._output.getvalue()

    def test_pending_lists_staged_writes(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            store = self._store(tmp)
            outcome = store.submit(
                SkillWriteRequest(action="create", skill_name="demo", content=_BODY)
            )
            loop = self._loop(store)

            loop._handle_skills_command(None)

            self.assertIn(outcome.pending_id, self._printed(loop))

    def test_approve_applies_and_reports(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            store = self._store(tmp)
            outcome = store.submit(
                SkillWriteRequest(action="create", skill_name="demo", content=_BODY)
            )
            loop = self._loop(store)

            loop._handle_skills_command(f"approve {outcome.pending_id}")

            self.assertTrue((tmp / "skills" / "demo" / "SKILL.md").is_file())
            self.assertEqual([], store.list_pending())

    def test_reject_drops_and_reports(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            store = self._store(tmp)
            outcome = store.submit(
                SkillWriteRequest(action="create", skill_name="demo", content=_BODY)
            )
            loop = self._loop(store)

            loop._handle_skills_command(f"reject {outcome.pending_id}")

            self.assertEqual([], store.list_pending())
            self.assertFalse((tmp / "skills" / "demo" / "SKILL.md").exists())

    def test_diff_prints_the_change(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            store = self._store(tmp)
            outcome = store.submit(
                SkillWriteRequest(action="create", skill_name="demo", content=_BODY)
            )
            loop = self._loop(store)

            loop._handle_skills_command(f"diff {outcome.pending_id}")

            self.assertIn("demo", self._printed(loop))

    def test_unknown_id_reports_error(self) -> None:
        with TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            loop = self._loop(self._store(tmp))

            loop._handle_skills_command("approve deadbeef")

            self.assertIn("deadbeef", self._printed(loop))

    def test_missing_store_reports_error(self) -> None:
        loop = self._loop(None)

        loop._handle_skills_command(None)

        self.assertIn("skills", self._printed(loop).lower())

    def test_empty_queue_reports_nothing_pending(self) -> None:
        with TemporaryDirectory() as tmpdir:
            loop = self._loop(self._store(Path(tmpdir)))

            loop._handle_skills_command(None)

            self.assertIn("待审", self._printed(loop))
