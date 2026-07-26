from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import textwrap
import unittest

from pickel.agents.skills import (
    SkillManifest,
    SkillRegistry,
    format_skill_catalog,
    compose_system_instruction,
    compose_system_instruction_parts,
)
from pickel.context.templates_loader import load_templates

# 与包内默认 skills_guidance.md（rstrip 后）字节一致
_DEFAULT_SKILLS_GUIDANCE = """You have access to filesystem-based skills.

Skills are modular capabilities discovered from metadata at startup. The catalog below only includes each skill's name, description, and location. Their full instructions are not loaded yet.

When a request matches a skill, first read that skill's SKILL.md from disk before following it. Only read additional files or execute bundled scripts if that skill's instructions reference them and they are necessary for the current task.

Load skills progressively. Do not read every skill up front or assume a skill applies unless its description matches the task."""


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
            # 显式传入包默认文案，避免本机 ~/.pickel/templates 覆盖影响断言
            instruction = compose_system_instruction(
                "You are Pickle.",
                manifests,
                skills_guidance=_DEFAULT_SKILLS_GUIDANCE,
            )

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
            parts = compose_system_instruction_parts(
                "You are Pickle.",
                manifests,
                skills_guidance=_DEFAULT_SKILLS_GUIDANCE,
            )

        self.assertEqual("You are Pickle.", parts.base_instruction)
        self.assertEqual(_DEFAULT_SKILLS_GUIDANCE, parts.skills_guidance)
        self.assertIn("Available skills:", parts.skills_catalog)
        self.assertIn("excel: Analyze spreadsheets.", parts.full_instruction)

    def test_compose_default_skills_guidance_matches_package_template(self) -> None:
        """无覆盖 home 时，默认 compose 的 guidance 与包内模板一致。"""
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
            package_default = load_templates(
                home=Path("/nonexistent-pickel-home-for-test")
            )["skills_guidance"]
            parts = compose_system_instruction_parts(
                "You are Pickle.",
                manifests,
                skills_guidance=package_default,
            )

        self.assertEqual(_DEFAULT_SKILLS_GUIDANCE, package_default)
        self.assertEqual(_DEFAULT_SKILLS_GUIDANCE, parts.skills_guidance)

    def test_compose_accepts_explicit_skills_guidance_override(self) -> None:
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
            parts = compose_system_instruction_parts(
                "You are Pickle.",
                manifests,
                skills_guidance="custom guidance text",
            )

        self.assertEqual("custom guidance text", parts.skills_guidance)
        self.assertIn("custom guidance text", parts.full_instruction)

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


class SkillManifestFieldTests(unittest.TestCase):
    def _write_skill(self, root: Path, name: str, frontmatter: str) -> Path:
        skill_dir = root / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\n{frontmatter}\n---\n\n# {name}\n", encoding="utf-8"
        )
        return skill_dir

    def test_new_fields_are_parsed(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_skill(
                root,
                "imagegen",
                "name: imagegen\ndescription: Generate images.\n"
                "version: 1.2.0\nstatus: stale\n"
                "required_env: [GEMINI_API_KEY]\nallowed_tools: [shell_exec]",
            )

            manifest = SkillRegistry.discover(root)[0]

            self.assertEqual("1.2.0", manifest.version)
            self.assertEqual("stale", manifest.status)
            self.assertEqual(("GEMINI_API_KEY",), manifest.required_env)
            self.assertEqual(("shell_exec",), manifest.allowed_tools)

    def test_missing_fields_fall_back_to_defaults(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_skill(root, "plain", "name: plain\ndescription: Plain skill.")

            manifest = SkillRegistry.discover(root)[0]

            self.assertEqual("", manifest.version)
            self.assertEqual("active", manifest.status)
            self.assertEqual((), manifest.required_env)
            self.assertEqual((), manifest.allowed_tools)

    def test_bad_values_fall_back_without_dropping_the_skill(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            self._write_skill(
                root,
                "weird",
                "name: weird\ndescription: Weird values.\n"
                "status: bogus\nversion: 12\nrequired_env: notalist",
            )

            manifests = SkillRegistry.discover(root)

            self.assertEqual(1, len(manifests))
            self.assertEqual("active", manifests[0].status)
            self.assertEqual("12", manifests[0].version)
            self.assertEqual((), manifests[0].required_env)


class SkillCatalogFormattingTests(unittest.TestCase):
    def _manifest(self, name: str, **kwargs) -> SkillManifest:
        return SkillManifest(
            name=name,
            description=f"{name} does things.",
            skill_dir=Path(f"/skills/{name}"),
            skill_file=Path(f"/skills/{name}/SKILL.md"),
            **kwargs,
        )

    def test_archived_skills_are_excluded(self) -> None:
        catalog = format_skill_catalog(
            [self._manifest("keep"), self._manifest("gone", status="archived")],
            environ={},
        )

        self.assertIn("keep", catalog)
        self.assertNotIn("gone", catalog)

    def test_stale_skills_are_marked(self) -> None:
        catalog = format_skill_catalog(
            [self._manifest("old", status="stale")], environ={}
        )

        self.assertIn("stale", catalog)

    def test_missing_required_env_is_marked_unavailable(self) -> None:
        catalog = format_skill_catalog(
            [self._manifest("imagegen", required_env=("GEMINI_API_KEY",))], environ={}
        )

        self.assertIn("unavailable", catalog)
        self.assertIn("GEMINI_API_KEY", catalog)

    def test_satisfied_required_env_is_not_marked(self) -> None:
        catalog = format_skill_catalog(
            [self._manifest("imagegen", required_env=("GEMINI_API_KEY",))],
            environ={"GEMINI_API_KEY": "x"},
        )

        self.assertNotIn("unavailable", catalog)

    def test_version_is_appended_when_present(self) -> None:
        catalog = format_skill_catalog(
            [self._manifest("v", version="1.2.0")], environ={}
        )

        self.assertIn("v1.2.0", catalog)

    def test_plain_skill_entry_is_unchanged(self) -> None:
        catalog = format_skill_catalog([self._manifest("plain")], environ={})

        self.assertTrue(catalog.endswith("(read /skills/plain/SKILL.md)"))
