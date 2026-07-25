import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pickel.config.paths import (
    discover_project_root,
    home_dir,
    sessions_db_path,
)


class PathsTests(unittest.TestCase):
    def test_home_dir_defaults_to_dot_pickel_under_user_home(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PICKEL_HOME", None)
            with patch("pickel.config.paths.Path.home", return_value=Path("/tmp/user")):
                self.assertEqual(Path("/tmp/user") / ".pickel", home_dir())

    def test_home_dir_respects_pickel_home_env(self) -> None:
        with TemporaryDirectory() as tmpdir:
            override = Path(tmpdir) / "custom-home"
            with patch.dict(os.environ, {"PICKEL_HOME": str(override)}):
                self.assertEqual(override, home_dir())

    def test_sessions_db_path_is_under_home_dir(self) -> None:
        with TemporaryDirectory() as tmpdir:
            override = Path(tmpdir) / "pickel-home"
            with patch.dict(os.environ, {"PICKEL_HOME": str(override)}):
                self.assertEqual(override / "sessions.db", sessions_db_path())

    def test_discover_project_root_finds_dot_pickel(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "proj"
            nested = root / "a" / "b"
            nested.mkdir(parents=True)
            (root / ".pickel").mkdir()

            self.assertEqual(root.resolve(), discover_project_root(nested))

    def test_discover_project_root_finds_agents_directory(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "proj"
            nested = root / "src" / "pkg"
            nested.mkdir(parents=True)
            (root / "agents").mkdir()

            self.assertEqual(root.resolve(), discover_project_root(nested))

    def test_discover_project_root_returns_cwd_when_cwd_is_root(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "proj"
            root.mkdir()
            (root / ".pickel").mkdir()

            self.assertEqual(root.resolve(), discover_project_root(root))

    def test_discover_project_root_returns_none_when_not_found(self) -> None:
        with TemporaryDirectory() as tmpdir:
            leaf = Path(tmpdir) / "no-markers" / "deep"
            leaf.mkdir(parents=True)

            self.assertIsNone(discover_project_root(leaf))

    def test_discover_project_root_ignores_agents_file(self) -> None:
        """agents 必须是目录，同名文件不算项目根。"""
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "proj"
            nested = root / "sub"
            nested.mkdir(parents=True)
            (root / "agents").write_text("not a dir", encoding="utf-8")

            self.assertIsNone(discover_project_root(nested))


if __name__ == "__main__":
    unittest.main()
