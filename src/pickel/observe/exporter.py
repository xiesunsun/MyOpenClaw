"""将 Session 的只读观测数据导出为自包含 HTML。"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path

from pickel.conversations.session import Session
from pickel.observe.collector import collect_trajectory
from pickel.observe.html_report import render_html
from pickel.observe.trace_reader import read_trace
from pickel.runs.trace_sink import trace_path


def default_report_path(
    sessions: Iterable[Session],
    *,
    directory: Path | None = None,
) -> Path:
    """生成包含主 session id 的默认文件名。"""
    items = tuple(sessions)
    if not items:
        raise ValueError("没有可导出的会话")
    session_id = _safe_filename_part(items[0].session_id)
    suffix = f"-plus-{len(items) - 1}" if len(items) > 1 else ""
    return (directory or Path.cwd()) / f"pickel-observe-{session_id}{suffix}.html"


def export_html(
    sessions: Iterable[Session],
    *,
    out: Path | None = None,
    trace_path_resolver: Callable[[str], Path] = trace_path,
) -> Path:
    """导出一个或多个会话，返回绝对输出路径。"""
    items = tuple(sessions)
    if not items:
        raise ValueError("没有可导出的会话")
    destination = out.expanduser() if out is not None else default_report_path(items)
    trajectories = [
        collect_trajectory(
            item,
            enhancement=read_trace(trace_path_resolver(item.session_id)),
        )
        for item in items
    ]
    destination.write_text(
        render_html(
            trajectories,
            generated_at=datetime.now(timezone.utc).isoformat(),
        ),
        encoding="utf-8",
    )
    return destination.resolve()


def _safe_filename_part(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return normalized or "session"
