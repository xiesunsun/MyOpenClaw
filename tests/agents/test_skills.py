from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import textwrap
import unittest

from myopenclaw.agents.skills import (
    SkillRegistry,
    compose_system_instruction,
    compose_system_instruction_parts,
)


class SkillRegistryTests(unittest.TestCase):
    def test_discover_reads_skill_frontmatter_from_directory(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill_dir = root / "pdf-processing"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: pdf-processing
                    description: Extract text and tables from PDF files.
                    ---

                    # PDF Processing
                    """
                ),
                encoding="utf-8",
            )

            manifests = SkillRegistry.discover(root)

        self.assertEqual(1, len(manifests))
        self.assertEqual("pdf-processing", manifests[0].name)
        self.assertEqual(
            "Extract text and tables from PDF files.",
            manifests[0].description,
        )

    def test_discover_ignores_skill_without_required_metadata(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill_dir = root / "broken"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: broken
                    ---

                    # Broken
                    """
                ),
                encoding="utf-8",
            )

            manifests = SkillRegistry.discover(root)

        self.assertEqual([], manifests)

    def test_compose_system_instruction_adds_skill_guidance_and_catalog(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill_dir = root / "excel"
            skill_dir.mkdir(parents=True)
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: excel
                    description: Analyze spreadsheets.
                    ---
                    """
                ),
                encoding="utf-8",
            )

            manifests = SkillRegistry.discover(root)
            instruction = compose_system_instruction("You are Pickle.", manifests)

        self.assertIn("You are Pickle.", instruction)
        self.assertIn("You have access to filesystem-based skills.", instruction)
        self.assertIn("Available skills:", instruction)
        self.assertIn("excel: Analyze spreadsheets.", instruction)
        self.assertIn(skill_file.as_posix(), instruction)

    def test_compose_system_instruction_parts_separates_behavior_and_skills(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            skill_dir = root / "excel"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: excel
                    description: Analyze spreadsheets.
                    ---
                    """
                ),
                encoding="utf-8",
            )

            manifests = SkillRegistry.discover(root)
            parts = compose_system_instruction_parts("You are Pickle.", manifests)

        self.assertEqual("You are Pickle.", parts.base_instruction)
        self.assertIn("filesystem-based skills", parts.skills_guidance)
        self.assertIn("Available skills:", parts.skills_catalog)
        self.assertIn("excel: Analyze spreadsheets.", parts.full_instruction)

    def test_repo_local_skills_use_uppercase_entrypoints_and_trigger_descriptions(self) -> None:
        root = Path(__file__).resolve().parents[2] / ".agent" / "skills"

        for skill_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            filenames = {child.name for child in skill_dir.iterdir() if child.is_file()}
            self.assertIn("SKILL.md", filenames, skill_dir.as_posix())
            self.assertNotIn("skill.md", filenames, skill_dir.as_posix())

        manifests = SkillRegistry.discover(root)
        manifest_names = {manifest.name for manifest in manifests}

        self.assertTrue({"gemini-api-dev", "image-generator", "skill-creator"}.issubset(manifest_names))
        for manifest in manifests:
            self.assertTrue(
                manifest.description.startswith("Use when "),
                f"{manifest.name} should use a trigger-oriented description",
            )

    def test_image_generator_cli_help_does_not_require_sdk_import(self) -> None:
        script = (
            Path(__file__).resolve().parents[2]
            / ".agent"
            / "skills"
            / "image-generator"
            / "scripts"
            / "generate_image.py"
        )

        result = subprocess.run(
            ["uv", "run", "python", str(script), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("--prompt", result.stdout)
        self.assertIn("--output", result.stdout)

    def test_image_generator_skill_uses_absolute_script_path_in_main_command(self) -> None:
        skill_path = (
            Path(__file__).resolve().parents[2]
            / ".agent"
            / "skills"
            / "image-generator"
            / "SKILL.md"
        )
        content = skill_path.read_text(encoding="utf-8")

        self.assertIn(
            "uv run python /Users/ssunxie/code/myopenclaw/.agent/skills/image-generator/scripts/generate_image.py",
            content,
        )
        self.assertNotIn(
            "uv run python .agent/skills/image-generator/scripts/generate_image.py",
            content,
        )


if __name__ == "__main__":
    unittest.main()
