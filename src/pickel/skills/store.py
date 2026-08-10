"""skill 写入的唯一入口：校验 → 护栏 → 落盘或暂存 → 审批。

工具（skill_manage）与 CLI（/skills）都经这里，审批规则只有一处实现。
pending 队列落在 ~/.pickel/pending/skills/ —— 它在 S2 沙箱内是 tmpfs，
沙箱里的 shell 看不见也改不了，agent 无法自我批准。
"""

from __future__ import annotations

from dataclasses import dataclass
import difflib
import json
from pathlib import Path
import re
import time
from uuid import uuid4

from pickel.skills.guard import SkillGuardError, scan_skill_content

_SKILL_FILE = "SKILL.md"
_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_ACTIONS = ("create", "patch", "delete")


class SkillStoreError(Exception):
    pass


@dataclass(frozen=True)
class SkillWriteRequest:
    action: str
    skill_name: str
    content: str = ""
    old_text: str = ""
    new_text: str = ""


@dataclass(frozen=True)
class SkillWriteOutcome:
    applied: bool
    pending_id: str | None
    path: Path | None
    message: str


@dataclass(frozen=True)
class PendingWrite:
    pending_id: str
    action: str
    skill_name: str
    created_at: float
    agent_id: str


class SkillStore:
    def __init__(
        self,
        *,
        skills_path: Path,
        pending_dir: Path,
        write_approval: bool = True,
        guard: bool = True,
        agent_id: str = "",
    ) -> None:
        self.skills_path = Path(skills_path)
        self.pending_dir = Path(pending_dir)
        self.write_approval = write_approval
        self.guard = guard
        self.agent_id = agent_id

    # --- 提交 ---

    def submit(self, request: SkillWriteRequest) -> SkillWriteOutcome:
        self._validate(request)
        target = self._target_content(request)
        if self.guard and request.action != "delete":
            hit = scan_skill_content(target)
            if hit is not None:
                raise SkillGuardError(*hit)
        if not self.write_approval:
            path = self._apply(request.action, request.skill_name, target)
            return SkillWriteOutcome(
                applied=True,
                pending_id=None,
                path=path,
                message=f"Skill written to {path}",
            )
        pending_id = self._stage(request, target)
        return SkillWriteOutcome(
            applied=False,
            pending_id=pending_id,
            path=None,
            message=(
                f"Pending approval (id: {pending_id}). "
                f"The user must run /skills approve {pending_id}."
            ),
        )

    # --- 审批 ---

    def list_pending(self) -> list[PendingWrite]:
        if not self.pending_dir.is_dir():
            return []
        records = []
        for path in sorted(self.pending_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            records.append(
                PendingWrite(
                    pending_id=data["id"],
                    action=data["action"],
                    skill_name=data["skill_name"],
                    created_at=data["created_at"],
                    agent_id=data.get("agent_id", ""),
                )
            )
        return records

    def diff(self, pending_id: str) -> str:
        data = self._load_pending(pending_id)
        current = self._current_content(data["skill_name"])
        target = data["target"]
        lines = difflib.unified_diff(
            current.splitlines(keepends=True),
            target.splitlines(keepends=True),
            fromfile=f"a/{data['skill_name']}/{_SKILL_FILE}",
            tofile=f"b/{data['skill_name']}/{_SKILL_FILE}",
        )
        return "".join(lines) or "(no textual change)"

    def approve(self, pending_id: str) -> Path:
        data = self._load_pending(pending_id)
        path = self._apply(data["action"], data["skill_name"], data["target"])
        self._pending_path(pending_id).unlink()
        return path

    def reject(self, pending_id: str) -> None:
        self._load_pending(pending_id)
        self._pending_path(pending_id).unlink()

    # --- 内部 ---

    def _validate(self, request: SkillWriteRequest) -> None:
        if request.action not in _ACTIONS:
            raise SkillStoreError(
                f"Unknown action {request.action!r}; "
                f"expected one of {', '.join(_ACTIONS)}"
            )
        if not _NAME_PATTERN.match(request.skill_name):
            raise SkillStoreError(
                f"Invalid skill name {request.skill_name!r}; "
                "use lowercase letters, digits and hyphens (e.g. image-generator)"
            )
        if request.action == "create" and not request.content.strip():
            raise SkillStoreError("create requires non-empty content")
        if request.action == "patch" and not request.old_text:
            raise SkillStoreError("patch requires old_text")

    def _skill_file(self, skill_name: str) -> Path:
        return self.skills_path / skill_name / _SKILL_FILE

    def _current_content(self, skill_name: str) -> str:
        path = self._skill_file(skill_name)
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def _target_content(self, request: SkillWriteRequest) -> str:
        if request.action == "create":
            return request.content
        current = self._current_content(request.skill_name)
        if not current:
            existing = ", ".join(self._existing_names()) or "(none)"
            raise SkillStoreError(
                f"Skill {request.skill_name!r} does not exist. "
                f"Existing skills: {existing}"
            )
        if request.action == "delete":
            return ""
        occurrences = current.count(request.old_text)
        if occurrences != 1:
            raise SkillStoreError(
                f"old_text must match exactly once; it matched {occurrences} times"
            )
        return current.replace(request.old_text, request.new_text, 1)

    def _existing_names(self) -> list[str]:
        if not self.skills_path.is_dir():
            return []
        return sorted(
            child.name
            for child in self.skills_path.iterdir()
            if (child / _SKILL_FILE).is_file()
        )

    def _apply(self, action: str, skill_name: str, target: str) -> Path:
        path = self._skill_file(skill_name)
        try:
            if action == "delete":
                if path.is_file():
                    path.unlink()
                skill_dir = path.parent
                if skill_dir.is_dir() and not any(skill_dir.iterdir()):
                    skill_dir.rmdir()
                return path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(target, encoding="utf-8")
        except OSError as exc:
            # 配置里的 skills 目录可能指向别的机器的路径（跨机器带过来的
            # settings.json），原始 OSError 会一路冒到 CLI 顶层崩掉会话
            raise SkillStoreError(f"Cannot write skill to {path}: {exc}") from exc
        return path

    def _stage(self, request: SkillWriteRequest, target: str) -> str:
        try:
            self.pending_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SkillStoreError(
                f"Cannot create the pending queue at {self.pending_dir}: {exc}"
            ) from exc
        pending_id = uuid4().hex[:8]
        payload = {
            "id": pending_id,
            "action": request.action,
            "skill_name": request.skill_name,
            "target": target,
            "created_at": time.time(),
            "agent_id": self.agent_id,
        }
        try:
            self._pending_path(pending_id).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            raise SkillStoreError(f"Cannot stage the write: {exc}") from exc
        return pending_id

    def _pending_path(self, pending_id: str) -> Path:
        return self.pending_dir / f"{pending_id}.json"

    def _load_pending(self, pending_id: str) -> dict:
        path = self._pending_path(pending_id)
        if not path.is_file():
            known = (
                ", ".join(record.pending_id for record in self.list_pending())
                or "(none)"
            )
            raise SkillStoreError(
                f"Unknown pending id {pending_id!r}. Pending ids: {known}"
            )
        return json.loads(path.read_text(encoding="utf-8"))
