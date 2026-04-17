from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class SkillManifest:
    name: str
    description: str
    skill_dir: Path
    skill_file: Path


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

        return SkillManifest(
            name=name.strip(),
            description=description.strip(),
            skill_dir=skill_file.parent.resolve(),
            skill_file=skill_file.resolve(),
        )

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


SKILL_USAGE_GUIDANCE = """You have access to filesystem-based skills.

Skills are modular capabilities discovered from metadata at startup. The catalog below only includes each skill's name, description, and location. Their full instructions are not loaded yet.

When a request matches a skill, first read that skill's SKILL.md from disk before following it. Only read additional files or execute bundled scripts if that skill's instructions reference them and they are necessary for the current task.

Load skills progressively. Do not read every skill up front or assume a skill applies unless its description matches the task."""


def compose_system_instruction(
    behavior_instruction: str,
    skills: list[SkillManifest],
) -> str:
    return compose_system_instruction_parts(behavior_instruction, skills).full_instruction


def compose_system_instruction_parts(
    behavior_instruction: str,
    skills: list[SkillManifest],
) -> SystemInstructionParts:
    base_instruction = behavior_instruction.strip()
    if not skills:
        return SystemInstructionParts(
            base_instruction=base_instruction,
            skills_guidance="",
            skills_catalog="",
        )
    return SystemInstructionParts(
        base_instruction=base_instruction,
        skills_guidance=SKILL_USAGE_GUIDANCE,
        skills_catalog=format_skill_catalog(skills),
    )


def format_skill_catalog(skills: list[SkillManifest]) -> str:
    lines = ["Available skills:"]
    for skill in skills:
        lines.append(format_skill_catalog_entry(skill))
    return "\n".join(lines)


def format_skill_catalog_entry(skill: SkillManifest) -> str:
    return f"- {skill.name}: {skill.description} (read {skill.skill_file.as_posix()})"
