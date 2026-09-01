from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typer.testing import CliRunner

from pickel.cli import main
from pickel.cli.main import app
from pickel.model_calls.content_store import InMemoryModelCallContentStore

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    """去掉 ANSI 转义；部分 CI 环境会强制 rich 输出着色文本。"""
    return _ANSI_RE.sub("", output)


def test_cli_observe_help() -> None:
    result = runner.invoke(app, ["observe", "--help"])
    assert result.exit_code == 0
    output = _plain(result.output)
    assert "可观测系统与故障诊断数据工作台" in output or "observe" in output
    assert "operation" in output


def test_cli_observe_operation_help() -> None:
    result = runner.invoke(app, ["observe", "operation", "--help"])
    assert result.exit_code == 0
    output = _plain(result.output)
    assert "导出单个 Operation 的诊断数据工作台" in output
    assert "--format" in output
    assert "--output" in output


def test_observe_operation_does_not_boot_runtime(monkeypatch, tmp_path: Path) -> None:
    """Operation 观测只能打开 Store，不能触发 Runtime/MCP 装载。"""

    class ReadOnlyStore:
        model_call_content_store = InMemoryModelCallContentStore()

    store = ReadOnlyStore()
    exported = tmp_path / "operation.json"
    calls: list[dict] = []
    monkeypatch.setattr(
        main,
        "_boot",
        lambda: (_ for _ in ()).throw(AssertionError("observe 不应启动 Runtime")),
    )
    monkeypatch.setattr(main, "runtime_db_path", lambda: tmp_path / "runtime.db")
    monkeypatch.setattr(main, "SQLiteRuntimeStore", lambda _path: store)
    monkeypatch.setattr(
        "pickel.observe.operation_report.export_operation_observation",
        lambda **kwargs: (calls.append(kwargs) or kwargs["out"]),
    )

    main.observe_operation("operation-1", format="json", output=exported)

    assert calls[0]["store"] is store
    assert calls[0]["content_store"] is store.model_call_content_store
    assert calls[0]["out"] == exported


def test_observe_operation_reports_old_schema_without_traceback(
    monkeypatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "runtime.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA user_version = 11")

    monkeypatch.setattr(main, "runtime_db_path", lambda: db_path)
    result = runner.invoke(app, ["observe", "operation", "operation-1"])

    assert result.exit_code == 1
    assert "schema version 11" in result.output
    assert "Traceback" not in result.output
    assert "Tavily MCP server running" not in result.output
