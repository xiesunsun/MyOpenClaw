"""事件 JSONL 落盘：派生的可观测轨迹。

红线 5：trace 不是对话事实的真源。本模块只写不读——不提供任何
load/replay 接口，禁止任何代码从 trace 重建对话或用量。
真源始终是 Session entry + metadata.usage。

红线 6：默认关闭。工具参数与文件内容会进入 trace。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TextIO

from pickel.config.paths import home_dir
from pickel.runs.runtime_events import RuntimeEventBase

TRACE_ENV_VAR = "PICKEL_TRACE"


def trace_enabled(config_value: bool = False) -> bool:
    """AppConfig.trace_enabled（默认 false），PICKEL_TRACE 覆盖之。"""
    override = os.environ.get(TRACE_ENV_VAR)
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes", "on"}
    return bool(config_value)


def trace_path(session_id: str) -> Path:
    return home_dir() / "traces" / f"{session_id}.jsonl"


class JsonlTraceSink:
    """一行一个事件；作为 EventBus 订阅者直接调用。"""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle: TextIO = self._path.open("a", encoding="utf-8")

    def __call__(self, event: RuntimeEventBase) -> None:
        self._handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        self._handle.flush()

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()
