"""CLI 非执行路径的导入边界回归。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_MODULE_PROBE = r"""
import runpy
import sys

sys.argv = {argv!r}
try:
    runpy.run_module("pickel.cli.main", run_name="__main__")
except SystemExit:
    pass

blocked = sorted(
    name
    for name in sys.modules
    if name == "anthropic"
    or name == "google"
    or name.startswith("google.genai")
    or name.startswith("pickel.providers")
    or name.startswith("pickel.runtime")
    or name.startswith("pickel.app.runtime_host")
)
print(json.dumps(blocked))
"""


def _modules_loaded_after_cli(*arguments: str, cwd: Path) -> list[str]:
    code = "import json\n" + _MODULE_PROBE.format(argv=["pickel", *arguments])
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_help_does_not_import_runtime_or_provider_sdk() -> None:
    loaded = _modules_loaded_after_cli("--help", cwd=Path.cwd())
    assert loaded == []


def test_invalid_query_does_not_import_runtime_or_provider_sdk() -> None:
    loaded = _modules_loaded_after_cli("--query", "", cwd=Path.cwd())
    assert loaded == []


def test_read_only_command_does_not_import_runtime_or_provider_sdk(
    tmp_path: Path,
) -> None:
    # 缺少数据库时命令会输出用户可读错误，但仍应保持只读导入边界。
    loaded = _modules_loaded_after_cli(
        "observe",
        "operation",
        "missing-operation",
        "--format",
        "json",
        cwd=tmp_path,
    )
    assert loaded == []
