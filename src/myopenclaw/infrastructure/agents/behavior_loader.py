from pathlib import Path

import yaml


class BehaviorLoader:
    CANDIDATE_FILES = ("AGENT.md", "agent.md")

    @classmethod
    def load(cls, behavior_path: Path) -> str:
        behavior_file = cls.resolve_file(behavior_path)
        raw_document = behavior_file.read_text(encoding="utf-8")
        return cls._strip_frontmatter(raw_document).strip()

    @classmethod
    def resolve_file(cls, behavior_path: Path) -> Path:
        if behavior_path.is_file():
            return behavior_path

        if not behavior_path.exists():
            raise FileNotFoundError(f"Behavior path not found: {behavior_path}")
        if not behavior_path.is_dir():
            raise ValueError(f"Behavior path must be a directory or file: {behavior_path}")

        for candidate_name in cls.CANDIDATE_FILES:
            candidate = behavior_path / candidate_name
            if candidate.exists():
                return candidate

        raise FileNotFoundError(
            f"Missing behavior file under {behavior_path}. Expected one of: {', '.join(cls.CANDIDATE_FILES)}"
        )

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        if not content.startswith("---\n"):
            return content

        end_delimiter = content.find("\n---\n", 4)
        if end_delimiter == -1:
            return content

        metadata_text = content[4:end_delimiter]
        yaml.safe_load(metadata_text)
        return content[end_delimiter + 5 :]
