"""templates_loader：包默认、用户覆盖、项目覆盖。"""

from __future__ import annotations

from pathlib import Path

from pickel.templates.loader import load_templates

# 与 src/pickel/templates/skills_guidance.md 一致（rstrip 后）
_DEFAULT_SKILLS_GUIDANCE = """You have access to filesystem-based skills.

Skills are modular capabilities discovered from metadata at startup. The catalog below only includes each skill's name, description, and location. Their full instructions are not loaded yet.

When a request matches a skill, first read that skill's SKILL.md from disk before following it. Only read additional files or execute bundled scripts if that skill's instructions reference them and they are necessary for the current task.

Load skills progressively. Do not read every skill up front or assume a skill applies unless its description matches the task."""


def test_default_load_has_skills_guidance_from_package():
    templates = load_templates(home=Path("/nonexistent-pickel-home-for-test"))
    assert "skills_guidance" in templates
    assert templates["skills_guidance"] == _DEFAULT_SKILLS_GUIDANCE


def test_user_home_override_replaces_skills_guidance(tmp_path: Path):
    home = tmp_path / "home"
    templates_dir = home / "templates"
    templates_dir.mkdir(parents=True)
    (templates_dir / "skills_guidance.md").write_text(
        "user override guidance\n", encoding="utf-8"
    )

    templates = load_templates(home=home)
    assert templates["skills_guidance"] == "user override guidance"


def test_project_override_wins_over_user(tmp_path: Path):
    home = tmp_path / "home"
    home_templates = home / "templates"
    home_templates.mkdir(parents=True)
    (home_templates / "skills_guidance.md").write_text(
        "user override\n", encoding="utf-8"
    )

    project_root = tmp_path / "project"
    project_templates = project_root / ".pickel" / "templates"
    project_templates.mkdir(parents=True)
    (project_templates / "skills_guidance.md").write_text(
        "project override\n", encoding="utf-8"
    )

    templates = load_templates(home=home, project_root=project_root)
    assert templates["skills_guidance"] == "project override"
