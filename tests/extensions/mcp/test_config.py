import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from pickel.extensions.mcp.config import load_mcp_servers


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


class LoadMcpServersTests(unittest.TestCase):
    def test_missing_files_yield_empty(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                {}, load_mcp_servers(home=root / "home", project_root=root / "proj")
            )

    def test_project_overrides_global_same_name(self) -> None:
        with TemporaryDirectory() as tmp:
            home, proj = Path(tmp) / "home", Path(tmp) / "proj"
            home.mkdir()
            proj.mkdir()
            _write(home / ".mcp.json", {"mcpServers": {
                "github": {"command": "global-cmd"},
                "jira": {"command": "jira-cmd", "args": ["--x"]},
            }})
            _write(proj / ".mcp.json", {"mcpServers": {
                "github": {"command": "project-cmd"},
            }})

            servers = load_mcp_servers(home=home, project_root=proj)

            self.assertEqual("project-cmd", servers["github"].command)
            self.assertEqual(("--x",), servers["jira"].args)

    def test_invalid_json_file_is_skipped_entirely(self) -> None:
        with TemporaryDirectory() as tmp:
            home, proj = Path(tmp) / "home", Path(tmp) / "proj"
            home.mkdir()
            proj.mkdir()
            (home / ".mcp.json").write_text("{not json", encoding="utf-8")
            _write(proj / ".mcp.json", {"mcpServers": {"ok": {"command": "c"}}})

            servers = load_mcp_servers(home=home, project_root=proj)

            self.assertEqual(["ok"], sorted(servers))

    def test_server_name_with_dunder_is_skipped(self) -> None:
        with TemporaryDirectory() as tmp:
            proj = Path(tmp)
            _write(proj / ".mcp.json", {"mcpServers": {
                "bad__name": {"command": "c"}, "good": {"command": "c"},
            }})

            servers = load_mcp_servers(home=proj / "nohome", project_root=proj)

            self.assertEqual(["good"], sorted(servers))

    def test_env_expands_vars_and_keeps_missing_literal(self) -> None:
        with TemporaryDirectory() as tmp:
            proj = Path(tmp)
            _write(proj / ".mcp.json", {"mcpServers": {
                "s": {"command": "c", "env": {
                    "TOKEN": "${PICKEL_TEST_TOKEN}",
                    "MISSING": "${PICKEL_TEST_NO_SUCH_VAR}",
                }},
            }})

            with mock.patch.dict("os.environ", {"PICKEL_TEST_TOKEN": "sekrit"}):
                servers = load_mcp_servers(home=proj / "nohome", project_root=proj)

            self.assertEqual("sekrit", servers["s"].env["TOKEN"])
            self.assertEqual("${PICKEL_TEST_NO_SUCH_VAR}", servers["s"].env["MISSING"])
