from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging
import os
from pathlib import Path

import yaml

from pickel.context.templates_loader import load_templates

logger = logging.getLogger(__name__)

_VALID_STATUSES = ("active", "stale", "archived")


@dataclass(frozen=True)
class SkillManifest:
    name: str
    description: str
    skill_dir: Path
    skill_file: Path
    version: str = ""
    status: str = "active"
    required_env: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class SystemInstructionParts:
    base_instruction: str
    skills_guidance: str
    skills_catalog: str

    @property
    def full_instruction(self) -> str:
        return "\n\n".join(
            section
            for section in [
                self.base_instruction,
                self.skills_guidance,
                self.skills_catalog,
            ]
            if section
        )


class SkillRegistry:
    CANDIDATE_FILES = ("SKILL.md", "skill.md")

    @classmethod
    def discover(cls, skills_path: Path | None) -> list[SkillManifest]:
        if skills_path is None:
            return []

        resolved_path = skills_path.resolve()
        if not resolved_path.exists():
            return []

        manifests: list[SkillManifest] = []
        for skill_file in cls._candidate_skill_files(resolved_path):
            manifest = cls._load_manifest(skill_file)
            if manifest is not None:
                manifests.append(manifest)

        return sorted(
            manifests,
            key=lambda manifest: (manifest.name.lower(), manifest.skill_dir.as_posix()),
        )

    @classmethod
    def _candidate_skill_files(cls, skills_path: Path) -> list[Path]:
        if skills_path.is_file():
            return [skills_path] if skills_path.name in cls.CANDIDATE_FILES else []
        if not skills_path.is_dir():
            return []

        candidates: list[Path] = []
        for candidate_name in cls.CANDIDATE_FILES:
            root_candidate = skills_path / candidate_name
            if root_candidate.exists():
                candidates.append(root_candidate)

        for child in sorted(skills_path.iterdir(), key=lambda item: item.as_posix()):
            if not child.is_dir():
                continue
            for candidate_name in cls.CANDIDATE_FILES:
                candidate = child / candidate_name
                if candidate.exists():
                    candidates.append(candidate)
                    break
        return candidates

    @classmethod
    def _load_manifest(cls, skill_file: Path) -> SkillManifest | None:
        metadata = cls._load_frontmatter(skill_file)
        if not isinstance(metadata, dict):
            return None

        name = metadata.get("name")
        description = metadata.get("description")
        if not isinstance(name, str) or not name.strip():
            return None
        if not isinstance(description, str) or not description.strip():
            return None

        status = metadata.get("status", "active")
        if status not in _VALID_STATUSES:
            logger.warning(
                "Skill '%s': unknown status %r; falling back to 'active'", name, status
            )
            status = "active"

        return SkillManifest(
            name=name.strip(),
            description=description.strip(),
            skill_dir=skill_file.parent.resolve(),
            skill_file=skill_file.resolve(),
            version=cls._coerce_str(metadata.get("version")),
            status=status,
            required_env=cls._coerce_str_tuple(
                metadata.get("required_env"), name, "required_env"
            ),
            allowed_tools=cls._coerce_str_tuple(
                metadata.get("allowed_tools"), name, "allowed_tools"
            ),
        )

    @staticmethod
    def _coerce_str(value: object) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @classmethod
    def _coerce_str_tuple(
        cls, value: object, skill_name: str, field: str
    ) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list):
            logger.warning(
                "Skill '%s': %s must be a list; ignoring %r", skill_name, field, value
            )
            return ()
        return tuple(str(item).strip() for item in value if str(item).strip())

    @staticmethod
    def _load_frontmatter(skill_file: Path) -> dict[str, object] | None:
        content = skill_file.read_text(encoding="utf-8")
        if not content.startswith("---\n"):
            return None

        end_delimiter = content.find("\n---\n", 4)
        if end_delimiter == -1:
            return None

        metadata_text = content[4:end_delimiter]
        try:
            metadata = yaml.safe_load(metadata_text)
        except yaml.YAMLError:
            return None
        return metadata if isinstance(metadata, dict) else None


def compose_system_instruction(
    behavior_instruction: str,
    skills: list[SkillManifest],
    *,
    skills_guidance: str | None = None,
) -> str:
    return compose_system_instruction_parts(
        behavior_instruction,
        skills,
        skills_guidance=skills_guidance,
    ).full_instruction


def compose_system_instruction_parts(
    behavior_instruction: str,
    skills: list[SkillManifest],
    *,
    skills_guidance: str | None = None,
) -> SystemInstructionParts:
    """组装 system instruction 分段。

    skills 非空时，skills_guidance 默认取 load_templates() 的 skills_guidance；
    也可由调用方显式传入以覆盖（便于测试与 ModelContext 构建管道注入）。
    """
    base_instruction = behavior_instruction.strip()
    if not skills:
        return SystemInstructionParts(
            base_instruction=base_instruction,
            skills_guidance="",
            skills_catalog="",
        )
    if skills_guidance is None:
        skills_guidance = load_templates()["skills_guidance"]
    return SystemInstructionParts(
        base_instruction=base_instruction,
        skills_guidance=skills_guidance,
        skills_catalog=format_skill_catalog(skills),
    )


def format_skill_catalog(
    skills: list[SkillManifest], *, environ: Mapping[str, str] | None = None
) -> str:
    resolved_env = os.environ if environ is None else environ
    lines = ["Available skills:"]
    for skill in skills:
        # archived 完全不进 catalog：它的存在只对人有意义
        if skill.status == "archived":
            continue
        lines.append(format_skill_catalog_entry(skill, environ=resolved_env))
    return "\n".join(lines)


def format_skill_catalog_entry(
    skill: SkillManifest, *, environ: Mapping[str, str] | None = None
) -> str:
    resolved_env = os.environ if environ is None else environ
    marks = []
    if skill.version:
        marks.append(f"v{skill.version}")
    if skill.status == "stale":
        marks.append("stale")
    missing = [name for name in skill.required_env if not resolved_env.get(name)]
    if missing:
        marks.append(f"unavailable: needs {', '.join(missing)}")
    suffix = f" ({'; '.join(marks)})" if marks else ""
    return (
        f"- {skill.name}: {skill.description} "
        f"(read {skill.skill_file.as_posix()}){suffix}"
    )
