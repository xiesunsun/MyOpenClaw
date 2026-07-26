# E1 Extension 宿主 — 实施计划

> **For agentic workers:** 按任务顺序实现，步骤用 checkbox 跟踪。设计依据：`docs/upgrade/2026-07-26-extension-host-design.md`；调研依据：`docs/upgrade/2026-07-26-tools-sandbox-research.md`。**前置：T1（`docs/upgrade/2026-07-26-tool-bus-implementation-plan.md`）必须先完成** —— Task 4 与 Task 8 依赖 `ToolBus` 与 `ToolSource.EXTENSION`。

**Goal:** 给 harness 装上插件宿主，四个扩展点（工具 / hook handler / recall source / session sync）可由 extension 贡献；把 openviking 从 core 完整摘出，成为第一个内置 extension。

**Architecture:** `ExtensionLoader` 发现并装载 → 每个 extension 拿到自己的 `ExtensionHost`（绑定名字与配置段）→ 贡献进 `ExtensionRegistry`。进程级扩展点（工具）注册实例，per-agent 扩展点（hook / recall / sync）注册工厂 `(AgentScope) -> X | None`，由 `Boot` 在 `build_run(agent_id)` / `build_session_service(agent_id)` 时求值。

**Tech Stack:** Python 3.12、importlib、pydantic、unittest（pytest 运行）。

## Global Constraints

- 测试命令：`uv run --with pytest --with pytest-asyncio pytest -q`。
- 基线：T1 完成后的失败集合 —— 6 例 `tests/tools/test_shell.py`（ANSI，属 S1）+ 12 例缺 API key 的 provider 初始化失败（分散在 `tests/app/test_assembly.py`、`tests/cli/test_chat_loop.py`、`tests/providers/`）。**判据是失败数恒为 18 且按文件的分布不变**，通过数只增不减。核对用 `pytest -q 2>&1 | grep FAILED | sed 's/::.*//' | sort | uniq -c`。
- 新模块首行 `from __future__ import annotations`。注释与 docstring 用中文，命名直接（`AGENTS.md`）。
- **包名区分**：`pickel.extensions_host`（core 侧宿主）与 `pickel.extensions`（内置 extension 存放处）。不要混。
- extension 的身份是发现时的目录名，不可自报；`ExtensionHost` 是 per-extension 实例，绑定该名字。
- 装载失败必须隔离：记错、跳过、其余继续，**不阻止 CLI 启动**。
- 每个任务结束时 `git commit`。

**OpenViking 生产服务（联调基线，2026-07-26 部署）**：endpoint `https://openviking.sunxie.me`，凭证在 `~/code/openviking/.env`（勿入库）；实测 API 契约表在 `~/code/openviking/README.md` 末尾——响应统一信封（业务数据在 `result` 里）、`commit` 异步需轮询 `tasks/{task_id}`、429 是配额常态需退避重试、`/messages` 与 `/messages/batch` 结构不同。**E1 范围内不改客户端代码**（验收=行为不变，走 SDK）；对真实服务的联调与契约修正是 E1 完成后的独立任务，以 README 表为准。

---

## 文件地图（目标）

| 路径 | 职责 | 状态 |
| --- | --- | --- |
| `src/pickel/conversations/session_sync.py` | `SessionSync` Protocol、`NoopSessionSync`、`CompositeSessionSync` | 新建（从集成层搬回 core） |
| `src/pickel/extensions_host/__init__.py` | 导出 `ExtensionHost` / `ExtensionRegistry` / `AgentScope` / `load_extensions` 与错误类型 | 新建 |
| `src/pickel/extensions_host/registry.py` | `AgentScope`、`ExtensionRegistry` | 新建 |
| `src/pickel/extensions_host/host.py` | `ExtensionHost`（四个 register + `config`） | 新建 |
| `src/pickel/extensions_host/loader.py` | 发现、import、调 `setup`、失败隔离与回滚 | 新建 |
| `src/pickel/extensions_host/errors.py` | `ExtensionLoadError`、`ExtensionConfigError` | 新建 |
| `src/pickel/extensions/__init__.py` | 内置 extension 包（空 docstring） | 新建 |
| `src/pickel/extensions/openviking/` | 从 `integrations/openviking/` 迁入 + `setup()` | 迁移 |
| `src/pickel/integrations/` | | Task 7a 后删空 |
| `src/pickel/config/app_config.py` | 增 `extensions` 段；删 `openviking` 字段与其 import | 改 |
| `src/pickel/config/loader.py` | `extensions` 段通用合并（settings + auth）；删 openviking 专段 | 改 |
| `src/pickel/config/migrate.py` | 旧 `openviking` 段折算进 `extensions.openviking`（settings 与 auth 两路） | 改 |
| `src/pickel/context/prepare.py` | `resolve_recalls` 异常隔离 | 改 |
| `src/pickel/app/boot.py` | 接 `ExtensionRegistry`；删四个 `_build_*` openviking 方法 | 改 |
| `src/pickel/cli/main.py` | `_boot()` 装载 extension | 改 |
| `src/pickel/cli/chat.py` | `/reload` 重载 extension | 改 |
| `config.yaml` | 顶层 `openviking:` → `extensions.openviking:` | 改 |
| `tests/extensions/openviking/` | 从 `tests/integrations/openviking/` 迁入 | 迁移 |

## 任务顺序与依赖

```text
Task 1  SessionSync 回归 core + CompositeSessionSync      （无依赖）
Task 2  AppConfig.extensions 段 + loader 合并 + auth       （无依赖）
Task 3  ExtensionHost / Registry / AgentScope              （依赖 1、2、T1）
Task 4  ExtensionLoader                                    （依赖 3）
Task 5  resolve_recalls 异常隔离                            （无依赖，可并行）
Task 6  Boot 接 registry（hook 首次接进生产路径）            （依赖 1、3、4）
Task 7a openviking 纯搬家（git mv + import 改）             （无依赖）
Task 7b openviking 的 setup() 与两个工厂（新增，未接线）      （依赖 3、7a）
Task 7c 原子切换：删 boot 三段 + AppConfig.openviking + 配置  （依赖 6、7b）
Task 8  CLI 装载入口 + /reload 重载                          （依赖 4、6）
Task 9  migrate 折算                                        （依赖 2、7c）
Task 10 验收                                                （依赖全部）
```

Task 7c 是**原子切换点**：在它之前 openviking 走旧的 `boot._build_*` 路径，在它之后走 extension 路径。不可拆开提交，否则会双重注册或功能中断。

---

## Task 1: SessionSync 回归 core + CompositeSessionSync

`SessionSync` Protocol 与 `NoopSessionSync` 现在定义在 `integrations/openviking/session_sync.py`，而 core 的 `conversations/service.py` 从那里 import 自己的协议 —— 反向依赖，必须先解。

**Files:**
- Create: `src/pickel/conversations/session_sync.py`
- Modify: `src/pickel/integrations/openviking/session_sync.py`（删 Protocol 与 Noop，改从 core import）
- Modify: `src/pickel/conversations/service.py`（改 import）
- Modify: `src/pickel/app/boot.py`（改 import）
- Test: `tests/conversations/test_session_sync.py`（新建）

**Interfaces:**
- Produces:
  - `SessionSync` Protocol：`sync_pending_messages(*, session)` / `commit_pending_messages(*, session, force=False)` / `delete_session(*, session)`
  - `NoopSessionSync`
  - `CompositeSessionSync(syncs: Sequence[SessionSync])` —— 逐个调用，单个异常隔离

- [x] **Step 1: 写失败测试**

创建 `tests/conversations/test_session_sync.py`（若 `tests/conversations/` 无 `__init__.py` 则一并创建空文件）：

```python
import unittest

from pickel.conversations.session import Session
from pickel.conversations.session_sync import CompositeSessionSync, NoopSessionSync


class _RecordingSync:
    def __init__(self, name: str, *, boom: bool = False) -> None:
        self.name = name
        self.boom = boom
        self.calls: list[str] = []

    def sync_pending_messages(self, *, session: Session) -> None:
        self.calls.append("sync")
        if self.boom:
            raise RuntimeError(f"{self.name} exploded")

    def commit_pending_messages(self, *, session: Session, force: bool = False) -> None:
        self.calls.append(f"commit:{force}")
        if self.boom:
            raise RuntimeError(f"{self.name} exploded")

    def delete_session(self, *, session: Session) -> None:
        self.calls.append("delete")
        if self.boom:
            raise RuntimeError(f"{self.name} exploded")


class CompositeSessionSyncTests(unittest.TestCase):
    def test_calls_every_sync_in_order(self) -> None:
        first = _RecordingSync("first")
        second = _RecordingSync("second")
        composite = CompositeSessionSync([first, second])
        session = Session.create(agent_id="Pickle")

        composite.sync_pending_messages(session=session)
        composite.commit_pending_messages(session=session, force=True)
        composite.delete_session(session=session)

        self.assertEqual(["sync", "commit:True", "delete"], first.calls)
        self.assertEqual(["sync", "commit:True", "delete"], second.calls)

    def test_one_failing_sync_does_not_stop_the_others(self) -> None:
        boom = _RecordingSync("boom", boom=True)
        healthy = _RecordingSync("healthy")
        composite = CompositeSessionSync([boom, healthy])
        session = Session.create(agent_id="Pickle")

        composite.sync_pending_messages(session=session)

        self.assertEqual(["sync"], healthy.calls)

    def test_empty_composite_is_equivalent_to_noop(self) -> None:
        composite = CompositeSessionSync([])
        session = Session.create(agent_id="Pickle")

        composite.sync_pending_messages(session=session)
        composite.commit_pending_messages(session=session)
        composite.delete_session(session=session)

    def test_noop_accepts_every_call(self) -> None:
        noop = NoopSessionSync()
        session = Session.create(agent_id="Pickle")

        noop.sync_pending_messages(session=session)
        noop.commit_pending_messages(session=session, force=True)
        noop.delete_session(session=session)


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: 跑测试确认失败**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/conversations/test_session_sync.py -q
```

Expected: FAIL，`ModuleNotFoundError: No module named 'pickel.conversations.session_sync'`

- [x] **Step 3: 实现 `src/pickel/conversations/session_sync.py`**

```python
"""会话同步协议与组合器。

协议定义在 core：core 的 SessionService 不应从集成层 import 自己的协议。
具体实现（如 OpenViking）由 extension 提供。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

from pickel.conversations.session import Session

logger = logging.getLogger(__name__)


class SessionSync(Protocol):
    def sync_pending_messages(self, *, session: Session) -> None: ...

    def commit_pending_messages(
        self,
        *,
        session: Session,
        force: bool = False,
    ) -> None: ...

    def delete_session(self, *, session: Session) -> None: ...


class NoopSessionSync:
    def sync_pending_messages(self, *, session: Session) -> None:
        return None

    def commit_pending_messages(
        self,
        *,
        session: Session,
        force: bool = False,
    ) -> None:
        return None

    def delete_session(self, *, session: Session) -> None:
        return None


class CompositeSessionSync:
    """把多个 extension 贡献的 sync 串起来。

    单个 sync 失败只记日志，不影响其余 sync，也不打断会话主流程 ——
    同步是旁路能力，不该让一个坏 extension 弄挂对话。
    """

    def __init__(self, syncs: Sequence[SessionSync]) -> None:
        self._syncs = list(syncs)

    def sync_pending_messages(self, *, session: Session) -> None:
        for sync in self._syncs:
            self._safe_call(sync, "sync_pending_messages", session=session)

    def commit_pending_messages(
        self,
        *,
        session: Session,
        force: bool = False,
    ) -> None:
        for sync in self._syncs:
            self._safe_call(
                sync,
                "commit_pending_messages",
                session=session,
                force=force,
            )

    def delete_session(self, *, session: Session) -> None:
        for sync in self._syncs:
            self._safe_call(sync, "delete_session", session=session)

    @staticmethod
    def _safe_call(sync: SessionSync, method: str, **kwargs) -> None:
        try:
            getattr(sync, method)(**kwargs)
        except Exception:
            logger.exception(
                "Session sync %s.%s failed",
                type(sync).__name__,
                method,
            )
```

- [x] **Step 4: 集成层改为从 core import**

`src/pickel/integrations/openviking/session_sync.py`：删掉 `SessionSync` Protocol 与 `NoopSessionSync` 两个定义，顶部改为

```python
from pickel.conversations.session_sync import NoopSessionSync, SessionSync
```

保留 `OpenVikingSessionSync`。若该文件不再用到 `Protocol`，从 `typing` import 里去掉。

- [x] **Step 5: 改 core 与 boot 的 import**

```bash
grep -rn "from pickel.integrations.openviking.session_sync import" src/ tests/ | grep -v __pycache__
```

对每个命中点：`SessionSync` / `NoopSessionSync` 改从 `pickel.conversations.session_sync` import；`OpenVikingSessionSync` 保持原路径。`conversations/service.py` 与 `app/boot.py` 都在此列。

- [x] **Step 6: 跑测试**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/conversations/ tests/integrations/ -q 2>&1 | tail -3
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
```

Expected: 新测试全过；全量失败清单不变。

- [x] **Step 7: Commit**

```bash
git add src/pickel/conversations/session_sync.py src/pickel/conversations/service.py \
        src/pickel/integrations/openviking/session_sync.py src/pickel/app/boot.py \
        tests/conversations/
git commit -m "refactor(conversations): SessionSync 协议回归 core，新增 CompositeSessionSync"
```

---

## Task 2: `AppConfig.extensions` 段 + loader 合并 + auth

**Files:**
- Modify: `src/pickel/config/app_config.py`（增 `extensions` 字段，**暂不删** `openviking`）
- Modify: `src/pickel/config/loader.py`（`extensions` 段通用合并）
- Test: `tests/config/test_extensions_config.py`（新建）

**Interfaces:**
- Produces: `AppConfig.extensions: dict[str, dict[str, Any]]`；loader 把 settings 的 `extensions.<name>` 与 `auth.json` 的 `extensions.<name>` 深合并（auth 优先）

- [x] **Step 1: 写失败测试**

创建 `tests/config/test_extensions_config.py`：

```python
"""extensions 段：settings 与 auth.json 深合并，auth 优先。"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pickel.config.loader import Config


class ExtensionsConfigTests(unittest.TestCase):
    def test_settings_and_auth_sections_merge_with_auth_winning(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".pickel").mkdir(parents=True)
            settings = {
                "default_agent": "Pickle",
                "default_llm": {"provider": "google/gemini", "model": "gemini-3-flash-preview"},
                "agents": {
                    "Pickle": {
                        "workspace_path": ".",
                        "behavior_path": ".",
                        "llm": {"provider": "google/gemini", "model": "gemini-3-flash-preview"},
                    }
                },
                "extensions": {
                    "openviking": {"enabled": True, "base_url": "https://ov.example", "user_key": "from-settings"}
                },
            }
            auth = {
                "extensions": {
                    "openviking": {"user_key": "from-auth", "account_id": "acct-1"}
                }
            }
            (home / ".pickel" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
            (home / ".pickel" / "auth.json").write_text(json.dumps(auth), encoding="utf-8")

            config = Config.load(cwd=Path(tmp), home=home / ".pickel")

            section = config.extensions["openviking"]
            self.assertEqual("from-auth", section["user_key"])      # auth 覆盖 settings
            self.assertEqual("acct-1", section["account_id"])        # auth 独有的键保留
            self.assertEqual("https://ov.example", section["base_url"])  # settings 独有的键保留
            self.assertTrue(section["enabled"])

    def test_extensions_defaults_to_empty_dict(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp) / "home" / ".pickel"
            home.mkdir(parents=True)
            settings = {
                "default_agent": "Pickle",
                "default_llm": {"provider": "google/gemini", "model": "gemini-3-flash-preview"},
                "agents": {
                    "Pickle": {
                        "workspace_path": ".",
                        "behavior_path": ".",
                        "llm": {"provider": "google/gemini", "model": "gemini-3-flash-preview"},
                    }
                },
            }
            (home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

            config = Config.load(cwd=Path(tmp), home=home)

            self.assertEqual({}, config.extensions)


if __name__ == "__main__":
    unittest.main()
```

先跑一次确认 `Config.load(cwd=..., home=...)` 的入参形态与断言中的配置最小集能通过校验；若 `AppConfig` 还要求别的必填键，按报错补进 `settings` 字典（不要改 `AppConfig` 的必填性）。

- [x] **Step 2: 跑测试确认失败**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/config/test_extensions_config.py -q
```

Expected: FAIL，`AttributeError: 'AppConfig' object has no attribute 'extensions'`

- [x] **Step 3: 加 `AppConfig.extensions`**

`src/pickel/config/app_config.py`，在 `openviking` 字段附近加：

```python
    # extension 的原始配置段：core 不认识任何 extension 的配置模型，
    # 解析由 extension 自己做（ExtensionHost.config）
    extensions: dict[str, dict[str, Any]] = Field(default_factory=dict)
```

`openviking` 字段与其 import **本任务保留**（Task 7c 才删），保证中间状态可跑。

- [x] **Step 4: loader 通用合并**

`src/pickel/config/loader.py`，在现有 openviking 专段（约 line 89-99）**之后**插入：

```python
        # settings 的 extensions.<name> 与 auth.json 的 extensions.<name> 深合并，auth 优先
        extensions: dict[str, Any] = dict(merged.get("extensions") or {})
        auth_extensions = global_auth.get("extensions") or {}
        if isinstance(auth_extensions, dict):
            for name, auth_section in auth_extensions.items():
                if isinstance(auth_section, dict):
                    extensions[name] = deep_merge(
                        extensions.get(name) or {},
                        auth_section,
                    )
        merged["extensions"] = extensions
```

openviking 专段本任务保留，Task 7c 删。

- [x] **Step 5: 跑测试**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/config/ -q 2>&1 | tail -3
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
```

Expected: 新测试全过；全量失败清单不变。

- [x] **Step 6: Commit**

```bash
git add src/pickel/config/app_config.py src/pickel/config/loader.py tests/config/test_extensions_config.py
git commit -m "feat(config): extensions 配置段，settings 与 auth 深合并"
```

---

## Task 3: `ExtensionHost` / `ExtensionRegistry` / `AgentScope`

**Files:**
- Create: `src/pickel/extensions_host/__init__.py`、`errors.py`、`registry.py`、`host.py`
- Test: `tests/extensions_host/test_host.py`（新建，含 `__init__.py`）

**Interfaces:**
- Consumes: `ToolBus` / `ToolSource`（T1）、`Recall`、`SessionSync`、`AppConfig`
- Produces:
  - `ExtensionLoadError(Exception)`、`ExtensionConfigError(Exception)`
  - `AgentScope(agent_id: str, app_config: AppConfig)` — frozen dataclass
  - `ExtensionRegistry`：`hook_factories` / `recall_factories` / `sync_factories`（各 `list[Callable[[AgentScope], Any | None]]`）；`extension_names: list[str]`；`hook_handlers(scope)` / `recall_sources(scope)` / `session_syncs(scope)` 三个求值方法（过滤 `None`、异常隔离）
  - `ExtensionHost(name, config_section, tool_bus, registry)`：`register_tool(tool)` / `add_hook_handler(factory)` / `add_recall_source(factory)` / `add_session_sync(factory)` / `config(model)`

- [x] **Step 1: 写失败测试**

创建 `tests/extensions_host/__init__.py`（空）与 `tests/extensions_host/test_host.py`：

```python
import unittest
from types import SimpleNamespace

from pydantic import BaseModel

from pickel.extensions_host.errors import ExtensionConfigError
from pickel.extensions_host.host import ExtensionHost
from pickel.extensions_host.registry import AgentScope, ExtensionRegistry
from pickel.tools.base import BaseTool, ToolSpec
from pickel.tools.bus import ToolBus, ToolSource


class _DemoConfig(BaseModel):
    enabled: bool = False
    base_url: str = ""


def _stub_tool(name: str) -> BaseTool:
    class _Stub(BaseTool):
        spec = ToolSpec(
            name=name,
            description=f"{name} description",
            input_schema={"type": "object", "properties": {}},
        )

    return _Stub()


def _host(*, name: str = "demo", section: dict | None = None) -> tuple[ExtensionHost, ToolBus, ExtensionRegistry]:
    bus = ToolBus()
    registry = ExtensionRegistry()
    host = ExtensionHost(
        name=name,
        config_section=section,
        tool_bus=bus,
        registry=registry,
    )
    return host, bus, registry


def _scope() -> AgentScope:
    return AgentScope(agent_id="Pickle", app_config=SimpleNamespace())


class ExtensionHostToolTests(unittest.TestCase):
    def test_register_tool_lands_in_bus_under_extension_origin(self) -> None:
        host, bus, _ = _host(name="openviking")

        host.register_tool(_stub_tool("recall_search"))

        entry = bus.get("ext__openviking__recall_search")
        self.assertEqual(ToolSource.EXTENSION, entry.source)
        self.assertEqual("openviking", entry.origin)


class ExtensionHostConfigTests(unittest.TestCase):
    def test_config_parses_own_section(self) -> None:
        host, _, _ = _host(section={"enabled": True, "base_url": "https://x"})

        config = host.config(_DemoConfig)

        self.assertTrue(config.enabled)
        self.assertEqual("https://x", config.base_url)

    def test_config_returns_none_when_section_absent(self) -> None:
        host, _, _ = _host(section=None)

        self.assertIsNone(host.config(_DemoConfig))

    def test_invalid_section_raises_extension_config_error(self) -> None:
        host, _, _ = _host(section={"enabled": "not-a-bool-at-all"})

        with self.assertRaises(ExtensionConfigError):
            host.config(_DemoConfig)


class ExtensionRegistryTests(unittest.TestCase):
    def test_factories_are_evaluated_with_scope_and_none_filtered(self) -> None:
        host, _, registry = _host()
        host.add_recall_source(lambda scope: f"recall-{scope.agent_id}")
        host.add_recall_source(lambda scope: None)

        sources = registry.recall_sources(_scope())

        self.assertEqual(["recall-Pickle"], sources)

    def test_failing_factory_is_skipped_without_breaking_others(self) -> None:
        host, _, registry = _host()

        def _boom(scope: AgentScope):
            raise RuntimeError("factory exploded")

        host.add_hook_handler(_boom)
        host.add_hook_handler(lambda scope: "healthy-handler")

        handlers = registry.hook_handlers(_scope())

        self.assertEqual(["healthy-handler"], handlers)

    def test_session_syncs_preserve_registration_order(self) -> None:
        host, _, registry = _host()
        host.add_session_sync(lambda scope: "first")
        host.add_session_sync(lambda scope: "second")

        self.assertEqual(["first", "second"], registry.session_syncs(_scope()))

    def test_registry_records_extension_names(self) -> None:
        bus = ToolBus()
        registry = ExtensionRegistry()
        for name in ("alpha", "beta"):
            ExtensionHost(
                name=name,
                config_section=None,
                tool_bus=bus,
                registry=registry,
            ).add_recall_source(lambda scope: None)

        self.assertEqual(["alpha", "beta"], registry.extension_names)


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: 跑测试确认失败**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/extensions_host/ -q
```

Expected: FAIL，`ModuleNotFoundError: No module named 'pickel.extensions_host'`

- [x] **Step 3: 实现 `errors.py`**

```python
"""Extension 宿主的错误类型。"""

from __future__ import annotations


class ExtensionLoadError(Exception):
    """extension 发现或装载失败（import 失败、缺 setup、setup 抛异常）。"""


class ExtensionConfigError(Exception):
    """extension 的配置段校验失败。"""
```

- [x] **Step 4: 实现 `registry.py`**

```python
"""Extension 贡献的收集与求值。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import logging
from typing import Any

logger = logging.getLogger(__name__)

# per-agent 扩展点注册的是工厂：返回 None 表示该 agent 不启用这项贡献
Factory = Callable[["AgentScope"], Any]


@dataclass(frozen=True)
class AgentScope:
    """工厂求值时的 agent 上下文。

    工厂拿不到 Run / Session —— 求值时它们还不存在。
    需要会话级状态的 extension 只能在 hook handler 的事件里拿。
    """

    agent_id: str
    app_config: Any


@dataclass
class ExtensionRegistry:
    """宿主收集到的全部贡献。Boot 从这里取。

    工具不在此列 —— 它们装载时就直接进了 ToolBus（进程级）。
    """

    hook_factories: list[Factory] = field(default_factory=list)
    recall_factories: list[Factory] = field(default_factory=list)
    sync_factories: list[Factory] = field(default_factory=list)
    extension_names: list[str] = field(default_factory=list)

    def note_extension(self, name: str) -> None:
        if name not in self.extension_names:
            self.extension_names.append(name)

    def hook_handlers(self, scope: AgentScope) -> list[Any]:
        return self._evaluate(self.hook_factories, scope, "hook handler")

    def recall_sources(self, scope: AgentScope) -> list[Any]:
        return self._evaluate(self.recall_factories, scope, "recall source")

    def session_syncs(self, scope: AgentScope) -> list[Any]:
        return self._evaluate(self.sync_factories, scope, "session sync")

    @staticmethod
    def _evaluate(factories: list[Factory], scope: AgentScope, label: str) -> list[Any]:
        """逐个求值，过滤 None，单个失败只记日志。"""
        results: list[Any] = []
        for factory in factories:
            try:
                value = factory(scope)
            except Exception:
                logger.exception("Extension %s factory failed", label)
                continue
            if value is not None:
                results.append(value)
        return results
```

- [x] **Step 5: 实现 `host.py`**

```python
"""给 extension 用的宿主 API。

ExtensionHost 是 per-extension 实例：loader 为每个 extension 单独构造一个，
绑定它的名字与它那段配置。extension 因此不需要（也无法）自报名字。
"""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from pickel.extensions_host.errors import ExtensionConfigError
from pickel.extensions_host.registry import ExtensionRegistry, Factory
from pickel.tools.base import BaseTool
from pickel.tools.bus import ToolBus, ToolSource

T = TypeVar("T", bound=BaseModel)


class ExtensionHost:
    def __init__(
        self,
        *,
        name: str,
        config_section: dict[str, Any] | None,
        tool_bus: ToolBus,
        registry: ExtensionRegistry,
    ) -> None:
        self.name = name
        self._config_section = config_section
        self._tool_bus = tool_bus
        self._registry = registry
        registry.note_extension(name)

    def config(self, model: type[T]) -> T | None:
        """按本 extension 的名字取配置段，用给定模型解析。

        段不存在 → None（extension 据此决定用默认值还是不启用）；
        段存在但校验失败 → ExtensionConfigError（由 loader 按装载失败隔离）。
        """
        if self._config_section is None:
            return None
        try:
            return model.model_validate(self._config_section)
        except ValidationError as exc:
            raise ExtensionConfigError(
                f"Invalid config for extension '{self.name}': {exc}"
            ) from exc

    def register_tool(self, tool: BaseTool) -> str:
        """注册进程级工具。最终名为 ext__<extension>__<tool>。"""
        return self._tool_bus.register(
            tool,
            source=ToolSource.EXTENSION,
            origin=self.name,
        )

    def add_hook_handler(self, factory: Factory) -> None:
        """注册 hook handler 工厂：(AgentScope) -> handler | None。

        handler 只需实现感兴趣的方法（duck typing，见 hooks/lifecycle.py 的 _call）。
        """
        self._registry.hook_factories.append(factory)

    def add_recall_source(self, factory: Factory) -> None:
        """注册召回源工厂：(AgentScope) -> Recall | None。"""
        self._registry.recall_factories.append(factory)

    def add_session_sync(self, factory: Factory) -> None:
        """注册会话同步工厂：(AgentScope) -> SessionSync | None。"""
        self._registry.sync_factories.append(factory)
```

- [x] **Step 6: 实现 `__init__.py`**

```python
"""Extension 宿主：发现、装载并收集 extension 的贡献。

与 pickel.extensions（内置 extension 存放处）区分：本包是 core 侧宿主。
"""

from pickel.extensions_host.errors import ExtensionConfigError, ExtensionLoadError
from pickel.extensions_host.host import ExtensionHost
from pickel.extensions_host.registry import AgentScope, ExtensionRegistry

__all__ = [
    "AgentScope",
    "ExtensionConfigError",
    "ExtensionHost",
    "ExtensionLoadError",
    "ExtensionRegistry",
]
```

- [x] **Step 7: 跑测试**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/extensions_host/ -q
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
```

Expected: 新测试全过；全量失败清单不变。

- [x] **Step 8: Commit**

```bash
git add src/pickel/extensions_host/ tests/extensions_host/
git commit -m "feat(extensions): ExtensionHost 与贡献注册表"
```

---

## Task 4: `ExtensionLoader`

**Files:**
- Create: `src/pickel/extensions_host/loader.py`
- Create: `src/pickel/extensions/__init__.py`
- Modify: `src/pickel/extensions_host/__init__.py`（导出 `load_extensions`）
- Test: `tests/extensions_host/test_loader.py`

**Interfaces:**
- Produces:
  - `load_extensions(*, tool_bus, app_config, home=None) -> LoadResult`
  - `LoadResult(registry: ExtensionRegistry, errors: list[ExtensionLoadError])`
  - `async def load_extensions_async(...)` —— `setup` 可为 `async def`；同步入口内部 `asyncio.run`
  - `teardown_extensions(...)`：对已装载模块调可选的 `teardown(host)`

- [x] **Step 1: 写失败测试**

创建 `tests/extensions_host/test_loader.py`：

```python
"""发现与装载：用户级目录、失败隔离、同名覆盖、工具回滚。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from pickel.extensions_host.loader import load_extensions
from pickel.tools.bus import ToolBus, ToolSource


def _app_config(extensions: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(extensions=extensions or {})


def _write_extension(home: Path, name: str, body: str) -> None:
    ext_dir = home / "extensions" / name
    ext_dir.mkdir(parents=True)
    (ext_dir / "__init__.py").write_text(body, encoding="utf-8")


_RECALL_EXT = """
def setup(host):
    host.add_recall_source(lambda scope: f"recall-from-{host.name}")
"""

_TOOL_EXT = """
from pickel.tools.base import BaseTool, ToolSpec


class _Probe(BaseTool):
    spec = ToolSpec(
        name="probe",
        description="probe",
        input_schema={"type": "object", "properties": {}},
    )


def setup(host):
    host.register_tool(_Probe())
"""

_BROKEN_IMPORT_EXT = "raise RuntimeError('import time boom')\n"

_NO_SETUP_EXT = "VALUE = 1\n"

_SETUP_BOOM_AFTER_TOOL_EXT = """
from pickel.tools.base import BaseTool, ToolSpec


class _Probe(BaseTool):
    spec = ToolSpec(
        name="probe",
        description="probe",
        input_schema={"type": "object", "properties": {}},
    )


def setup(host):
    host.register_tool(_Probe())
    raise RuntimeError('setup boom')
"""

_ASYNC_EXT = """
async def setup(host):
    host.add_session_sync(lambda scope: f"sync-from-{host.name}")
"""


class LoaderDiscoveryTests(unittest.TestCase):
    def test_loads_user_level_extension_and_collects_contribution(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_extension(home, "demo", _RECALL_EXT)

            result = load_extensions(
                tool_bus=ToolBus(),
                app_config=_app_config(),
                home=home,
            )

            self.assertEqual([], result.errors)
            self.assertEqual(["demo"], result.registry.extension_names)
            scope = SimpleNamespace(agent_id="Pickle", app_config=None)
            self.assertEqual(["recall-from-demo"], result.registry.recall_sources(scope))

    def test_registers_tools_under_extension_origin(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_extension(home, "probe_ext", _TOOL_EXT)
            bus = ToolBus()

            result = load_extensions(tool_bus=bus, app_config=_app_config(), home=home)

            self.assertEqual([], result.errors)
            entry = bus.get("ext__probe_ext__probe")
            self.assertEqual(ToolSource.EXTENSION, entry.source)

    def test_awaits_async_setup(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_extension(home, "async_ext", _ASYNC_EXT)

            result = load_extensions(
                tool_bus=ToolBus(), app_config=_app_config(), home=home
            )

            self.assertEqual([], result.errors)
            scope = SimpleNamespace(agent_id="Pickle", app_config=None)
            self.assertEqual(
                ["sync-from-async_ext"], result.registry.session_syncs(scope)
            )

    def test_skips_underscore_and_dot_prefixed_entries(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_extension(home, "_private", _RECALL_EXT)
            _write_extension(home, ".hidden", _RECALL_EXT)
            _write_extension(home, "real", _RECALL_EXT)

            result = load_extensions(
                tool_bus=ToolBus(), app_config=_app_config(), home=home
            )

            self.assertEqual(["real"], result.registry.extension_names)

    def test_missing_extensions_dir_is_not_an_error(self) -> None:
        with TemporaryDirectory() as tmp:
            result = load_extensions(
                tool_bus=ToolBus(), app_config=_app_config(), home=Path(tmp)
            )

            self.assertEqual([], result.errors)


class LoaderIsolationTests(unittest.TestCase):
    def test_import_failure_is_isolated_and_others_still_load(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_extension(home, "aaa_broken", _BROKEN_IMPORT_EXT)
            _write_extension(home, "zzz_healthy", _RECALL_EXT)

            result = load_extensions(
                tool_bus=ToolBus(), app_config=_app_config(), home=home
            )

            self.assertEqual(1, len(result.errors))
            self.assertIn("aaa_broken", str(result.errors[0]))
            self.assertEqual(["zzz_healthy"], result.registry.extension_names)

    def test_module_without_setup_reports_a_clear_error(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_extension(home, "no_setup", _NO_SETUP_EXT)

            result = load_extensions(
                tool_bus=ToolBus(), app_config=_app_config(), home=home
            )

            self.assertEqual(1, len(result.errors))
            self.assertIn("setup", str(result.errors[0]))

    def test_setup_failure_rolls_back_already_registered_tools(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_extension(home, "half_dead", _SETUP_BOOM_AFTER_TOOL_EXT)
            bus = ToolBus()

            result = load_extensions(tool_bus=bus, app_config=_app_config(), home=home)

            self.assertEqual(1, len(result.errors))
            # 半装状态必须回滚，否则 bus 里留着一个无人维护的工具
            self.assertEqual([], bus.list_names(source=ToolSource.EXTENSION))

    def test_invalid_config_section_is_a_load_error(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_extension(
                home,
                "cfg_ext",
                "from pydantic import BaseModel\n"
                "class _C(BaseModel):\n"
                "    count: int = 0\n"
                "def setup(host):\n"
                "    host.config(_C)\n",
            )

            result = load_extensions(
                tool_bus=ToolBus(),
                app_config=_app_config({"cfg_ext": {"count": "not-an-int"}}),
                home=home,
            )

            self.assertEqual(1, len(result.errors))


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: 跑测试确认失败**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/extensions_host/test_loader.py -q
```

Expected: FAIL，`ModuleNotFoundError: No module named 'pickel.extensions_host.loader'`

- [x] **Step 3: 建内置 extension 包**

`src/pickel/extensions/__init__.py`：

```python
"""内置 extension 存放处。每个子目录一个 extension，暴露 setup(host)。"""
```

- [x] **Step 4: 实现 `loader.py`**

```python
"""Extension 发现与装载。

发现顺序：内置（pickel.extensions.*）→ 用户级（~/.pickel/extensions/*）。
同名时用户级覆盖内置 —— 允许用户就地替换内置实现。
任一 extension 装载失败都被隔离：记错、回滚它注册的工具、继续装其余。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import importlib
import importlib.util
import inspect
import logging
from pathlib import Path
import pkgutil
from types import ModuleType
from typing import Any

from pickel.config.paths import home_dir
from pickel.extensions_host.errors import ExtensionConfigError, ExtensionLoadError
from pickel.extensions_host.host import ExtensionHost
from pickel.extensions_host.registry import ExtensionRegistry
from pickel.tools.bus import ToolBus, ToolSource

logger = logging.getLogger(__name__)

_BUILTIN_PACKAGE = "pickel.extensions"
_USER_DIR_NAME = "extensions"


@dataclass
class LoadResult:
    registry: ExtensionRegistry = field(default_factory=ExtensionRegistry)
    errors: list[ExtensionLoadError] = field(default_factory=list)
    modules: dict[str, ModuleType] = field(default_factory=dict)


def load_extensions(
    *,
    tool_bus: ToolBus,
    app_config: Any,
    home: Path | None = None,
) -> LoadResult:
    """同步入口。setup 可以是 async def，内部 asyncio.run 承接。"""
    return asyncio.run(
        load_extensions_async(tool_bus=tool_bus, app_config=app_config, home=home)
    )


async def load_extensions_async(
    *,
    tool_bus: ToolBus,
    app_config: Any,
    home: Path | None = None,
) -> LoadResult:
    result = LoadResult()
    sections = getattr(app_config, "extensions", None) or {}

    for name, module_loader in _discover(home).items():
        try:
            module = module_loader()
        except Exception as exc:
            result.errors.append(
                ExtensionLoadError(f"Failed to import extension '{name}': {exc}")
            )
            logger.exception("Failed to import extension '%s'", name)
            continue

        setup = getattr(module, "setup", None)
        if setup is None:
            result.errors.append(
                ExtensionLoadError(
                    f"Extension '{name}' has no setup(host) function"
                )
            )
            continue

        host = ExtensionHost(
            name=name,
            config_section=sections.get(name),
            tool_bus=tool_bus,
            registry=result.registry,
        )
        try:
            outcome = setup(host)
            if inspect.isawaitable(outcome):
                await outcome
        except (ExtensionConfigError, Exception) as exc:
            # 回滚半装状态：它可能已经注册了一部分工具
            tool_bus.unregister_origin(ToolSource.EXTENSION, name)
            result.errors.append(
                ExtensionLoadError(f"Extension '{name}' setup failed: {exc}")
            )
            logger.exception("Extension '%s' setup failed", name)
            continue

        result.modules[name] = module

    return result


async def teardown_extensions(result: LoadResult, *, tool_bus: ToolBus) -> None:
    """卸载：调各 extension 可选的 teardown，并摘掉它们注册的工具。"""
    for name, module in result.modules.items():
        teardown = getattr(module, "teardown", None)
        if teardown is not None:
            try:
                outcome = teardown()
                if inspect.isawaitable(outcome):
                    await outcome
            except Exception:
                logger.exception("Extension '%s' teardown failed", name)
        tool_bus.unregister_origin(ToolSource.EXTENSION, name)


def _discover(home: Path | None) -> dict[str, Any]:
    """名字 → 返回模块的可调用。用户级同名覆盖内置。"""
    found: dict[str, Any] = {}

    for module_info in _iter_builtin_module_names():
        name = module_info
        found[name] = lambda n=name: importlib.import_module(f"{_BUILTIN_PACKAGE}.{n}")

    user_root = (home or home_dir()) / _USER_DIR_NAME
    for path in _iter_user_paths(user_root):
        name = path.stem if path.is_file() else path.name
        found[name] = lambda p=path, n=name: _import_from_path(n, p)

    return dict(sorted(found.items()))


def _iter_builtin_module_names() -> list[str]:
    try:
        package = importlib.import_module(_BUILTIN_PACKAGE)
    except ModuleNotFoundError:
        return []
    return [
        info.name
        for info in pkgutil.iter_modules(package.__path__)
        if not info.name.startswith("_")
    ]


def _iter_user_paths(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    paths: list[Path] = []
    for child in sorted(root.iterdir()):
        if child.name.startswith(("_", ".")):
            continue
        if child.is_dir() and (child / "__init__.py").is_file():
            paths.append(child)
        elif child.is_file() and child.suffix == ".py":
            paths.append(child)
    return paths


def _import_from_path(name: str, path: Path) -> ModuleType:
    target = path / "__init__.py" if path.is_dir() else path
    # 模块名加前缀，避免与已导入的同名模块撞
    spec = importlib.util.spec_from_file_location(f"pickel_ext_{name}", target)
    if spec is None or spec.loader is None:
        raise ExtensionLoadError(f"Cannot load extension '{name}' from {target}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
```

`except (ExtensionConfigError, Exception)` 里 `ExtensionConfigError` 是冗余的（它是 `Exception` 子类），写出来只为表达意图；实现时可简化为 `except Exception`，行为一致。

- [x] **Step 5: 导出 `load_extensions`**

`src/pickel/extensions_host/__init__.py` 增加：

```python
from pickel.extensions_host.loader import (
    LoadResult,
    load_extensions,
    load_extensions_async,
    teardown_extensions,
)
```

并把这四个名字加进 `__all__`。

- [x] **Step 6: 跑测试**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/extensions_host/ -q
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
```

Expected: 新测试全过；全量失败清单不变。

- [x] **Step 7: Commit**

```bash
git add src/pickel/extensions_host/loader.py src/pickel/extensions_host/__init__.py \
        src/pickel/extensions/__init__.py tests/extensions_host/test_loader.py
git commit -m "feat(extensions): ExtensionLoader 发现装载与失败隔离"
```

---

## Task 5: `resolve_recalls` 异常隔离

现状 `context/prepare.py` 的 `resolve_recalls` 无保护，任一 recall source 抛异常会冒泡打断整个 turn。extension 化后这条路径上会跑第三方代码，必须补。

**Files:**
- Modify: `src/pickel/context/prepare.py`
- Test: `tests/context/test_prepare.py`（追加）

- [x] **Step 1: 写失败测试**

`tests/context/test_prepare.py`（pytest 函数式）追加：

```python
def test_failing_recall_source_does_not_break_the_turn():
    import asyncio

    from pickel.context.prepare import resolve_recalls
    from pickel.conversations.agent_message import UserMessage
    from pickel.conversations.content_blocks import TextContent

    class _BoomRecall:
        async def provide(self, *, run, session, current_user_text=""):
            raise RuntimeError("recall exploded")

    class _HealthyRecall:
        async def provide(self, *, run, session, current_user_text=""):
            return [UserMessage(content=[TextContent(text="recalled")])]

    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="hi")]))

    messages = asyncio.run(
        resolve_recalls(
            messages=[],
            run=_run(),
            session=session,
            recall_sources=[_BoomRecall(), _HealthyRecall()],
        )
    )

    # 坏的被跳过，好的仍然生效
    assert len(messages) == 1
    assert messages[0].content[0].text == "recalled"
```

若 `_run()` helper 已在 Task 6 of T1 被改造为不带 `tools`，直接沿用其当前形态。

- [x] **Step 2: 跑测试确认失败**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/context/test_prepare.py -q -k recall
```

Expected: FAIL，`RuntimeError: recall exploded` 冒泡

- [x] **Step 3: 实现**

`src/pickel/context/prepare.py` 的 `resolve_recalls`，把循环体改为：

```python
    for source in recall_sources:
        try:
            provided = await source.provide(
                run=run,
                session=session,
                current_user_text=text,
            )
        except Exception:
            # recall 是旁路能力：单源失败不该打断 turn
            logger.exception(
                "Recall source %s failed",
                type(source).__name__,
            )
            continue
        result.extend(provided)
```

文件顶部加 `import logging` 与 `logger = logging.getLogger(__name__)`（若已有则复用）。

- [x] **Step 4: 跑测试**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/context/ -q 2>&1 | tail -3
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
```

Expected: 新测试通过；全量失败清单不变。

- [x] **Step 5: Commit**

```bash
git add src/pickel/context/prepare.py tests/context/test_prepare.py
git commit -m "fix(context): recall 单源失败不再打断 turn"
```

---

## Task 6: Boot 接 registry（hook 首次接进生产路径）

**Files:**
- Modify: `src/pickel/app/boot.py`
- Test: `tests/app/test_boot_extensions.py`（新建）

**Interfaces:**
- Consumes: `ExtensionRegistry`、`AgentScope`（Task 3）、`CompositeSessionSync`（Task 1）
- Produces:
  - `Boot.__init__(app_config, tool_bus=None, extensions: ExtensionRegistry | None = None)`
  - `Boot.from_config(app_config, tool_bus=None, extensions=None)`
  - `build_run` 传 `recall_sources` 与 `lifecycle_hooks=LifecycleHooks(handlers=...)`
  - `build_session_service` 用 `CompositeSessionSync`

- [x] **Step 1: 写失败测试**

创建 `tests/app/test_boot_extensions.py`：

```python
"""Boot 从 ExtensionRegistry 取贡献，并按 agent 求值。"""

import unittest
from types import SimpleNamespace

from pickel.app.boot import Boot
from pickel.extensions_host.registry import ExtensionRegistry


class BootExtensionWiringTests(unittest.TestCase):
    def test_registry_defaults_to_empty_when_not_injected(self) -> None:
        boot = Boot.from_config(SimpleNamespace(extensions={}))

        self.assertEqual([], boot.extensions.extension_names)

    def test_recall_factories_are_evaluated_per_agent(self) -> None:
        registry = ExtensionRegistry()
        seen: list[str] = []

        def _factory(scope):
            seen.append(scope.agent_id)
            return f"recall-{scope.agent_id}"

        registry.recall_factories.append(_factory)
        boot = Boot.from_config(SimpleNamespace(extensions={}), extensions=registry)

        sources = boot.resolve_recall_sources("Pickle")

        self.assertEqual(["recall-Pickle"], sources)
        self.assertEqual(["Pickle"], seen)

    def test_hook_handlers_come_from_registry(self) -> None:
        registry = ExtensionRegistry()
        registry.hook_factories.append(lambda scope: "handler-a")
        registry.hook_factories.append(lambda scope: None)
        boot = Boot.from_config(SimpleNamespace(extensions={}), extensions=registry)

        self.assertEqual(["handler-a"], boot.resolve_hook_handlers("Pickle"))

    def test_session_syncs_come_from_registry(self) -> None:
        registry = ExtensionRegistry()
        registry.sync_factories.append(lambda scope: "sync-a")
        boot = Boot.from_config(SimpleNamespace(extensions={}), extensions=registry)

        self.assertEqual(["sync-a"], boot.resolve_session_syncs("Pickle"))


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: 跑测试确认失败**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/app/test_boot_extensions.py -q
```

Expected: FAIL，`TypeError: Boot.from_config() got an unexpected keyword argument 'extensions'`

- [x] **Step 3: Boot 接受 registry**

`src/pickel/app/boot.py`：

```python
    def __init__(
        self,
        app_config: AppConfig,
        tool_bus: ToolBus | None = None,
        extensions: ExtensionRegistry | None = None,
    ) -> None:
        self.app_config = app_config
        if tool_bus is None:
            tool_bus = ToolBus()
            install_builtin_tools(tool_bus)
        self.tool_bus = tool_bus
        self.extensions = extensions or ExtensionRegistry()

    @classmethod
    def from_config(
        cls,
        app_config: AppConfig,
        tool_bus: ToolBus | None = None,
        extensions: ExtensionRegistry | None = None,
    ) -> "Boot":
        return cls(app_config, tool_bus=tool_bus, extensions=extensions)
```

顶部 import 增加：

```python
from pickel.conversations.session_sync import CompositeSessionSync
from pickel.extensions_host.registry import AgentScope, ExtensionRegistry
from pickel.hooks.lifecycle import LifecycleHooks
```

- [x] **Step 4: 加三个求值方法**

`Boot` 类内追加：

```python
    def _scope(self, agent_id: str | None) -> AgentScope:
        resolved = agent_id or self.app_config.default_agent
        return AgentScope(agent_id=resolved, app_config=self.app_config)

    def resolve_recall_sources(self, agent_id: str | None = None) -> list:
        return self.extensions.recall_sources(self._scope(agent_id))

    def resolve_hook_handlers(self, agent_id: str | None = None) -> list:
        return self.extensions.hook_handlers(self._scope(agent_id))

    def resolve_session_syncs(self, agent_id: str | None = None) -> list:
        return self.extensions.session_syncs(self._scope(agent_id))
```

`SimpleNamespace` 假 config 没有 `default_agent`，测试里都显式传了 agent_id，所以 `_scope` 里的 `or self.app_config.default_agent` 只在 agent_id 为 None 时才求值 —— 测试不会踩到。

- [x] **Step 5: `build_run` 用 registry 的贡献**

把 `build_run` 里的

```python
            recall_sources=self._build_recall_sources(agent_id=agent.agent_id),
```

改为

```python
            recall_sources=self.resolve_recall_sources(agent.agent_id)
            + self._build_recall_sources(agent_id=agent.agent_id),
            lifecycle_hooks=LifecycleHooks(
                handlers=self.resolve_hook_handlers(agent.agent_id)
            ),
```

`_build_recall_sources`（openviking 旧路径）**本任务保留并列**，Task 7c 删除后只剩 registry 一路。`Run.open` 的 `lifecycle_hooks` 参数已存在，此处首次在生产路径传入非 Noop 的实例。

- [x] **Step 6: `build_session_service` 用 Composite**

把

```python
        session_sync = self._build_session_sync(
            agent_id=agent_id,
            db_path=db_path,
        )
        return SessionService(repository, session_sync)
```

改为

```python
        syncs = list(self.resolve_session_syncs(agent_id))
        legacy_sync = self._build_session_sync(agent_id=agent_id, db_path=db_path)
        if not isinstance(legacy_sync, NoopSessionSync):
            syncs.append(legacy_sync)
        return SessionService(repository, CompositeSessionSync(syncs))
```

`NoopSessionSync` 从 `pickel.conversations.session_sync` import（Task 1 已迁）。Task 7c 删掉 `_build_session_sync` 后这里简化为 `CompositeSessionSync(self.resolve_session_syncs(agent_id))`。

- [x] **Step 7: 跑测试**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/app/ tests/integrations/ -q 2>&1 | tail -3
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
```

Expected: 新测试全过；全量失败清单不变（openviking 仍走旧路径，行为不变）。

- [x] **Step 8: Commit**

```bash
git add src/pickel/app/boot.py tests/app/test_boot_extensions.py
git commit -m "feat(app): Boot 接 ExtensionRegistry，LifecycleHooks 接进生产路径"
```

---

## Task 7a: openviking 纯搬家

只挪位置、只改 import 路径，功能与接线一律不动。

**Files:**
- Move: `src/pickel/integrations/openviking/` → `src/pickel/extensions/openviking/`
- Move: `tests/integrations/openviking/` → `tests/extensions/openviking/`
- Modify: 全仓库对旧路径的 import

- [x] **Step 1: 搬家**

```bash
git mv src/pickel/integrations/openviking src/pickel/extensions/openviking
git mv tests/integrations/openviking tests/extensions/openviking
```

`src/pickel/integrations/` 与 `tests/integrations/` 若只剩 `__init__.py`，一并删除：

```bash
git rm src/pickel/integrations/__init__.py tests/integrations/__init__.py
```

`tests/extensions/` 需要 `__init__.py`（与 `tests/extensions_host/` 同）：

```bash
touch tests/extensions/__init__.py && git add tests/extensions/__init__.py
```

- [x] **Step 2: 批量改 import 路径**

```bash
grep -rl "pickel\.integrations\.openviking" src/ tests/ | grep -v __pycache__ | \
  xargs sed -i 's/pickel\.integrations\.openviking/pickel.extensions.openviking/g'
```

确认无残留：

```bash
grep -rn "integrations" src/ tests/ | grep -v __pycache__
```

Expected: 无命中（`docs/openviking/` 下的文档不在此列，不改）。

- [x] **Step 3: 跑全量**

```bash
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
```

Expected: 全量失败清单不变。纯搬家不该改变任何行为。

- [x] **Step 4: Commit**

```bash
git add -A src/pickel tests/
git commit -m "refactor: openviking 集成迁入 extensions/ 目录（纯搬家）"
```

---

## Task 7b: openviking 的 `setup()` 与两个工厂

新增代码，暂不接线 —— `boot` 仍走旧路径，此任务只让 `setup()` 存在且可被单测调用。

**Files:**
- Modify: `src/pickel/extensions/openviking/__init__.py`
- Test: `tests/extensions/openviking/test_setup.py`

**Interfaces:**
- Produces: `setup(host: ExtensionHost) -> None`；内部两个工厂 `_make_recall(scope)` / `_make_session_sync(scope)`

- [x] **Step 1: 写失败测试**

创建 `tests/extensions/openviking/test_setup.py`：

```python
"""setup 只在 enabled 时注册贡献。"""

import unittest
from types import SimpleNamespace

from pickel.extensions.openviking import setup
from pickel.extensions_host.host import ExtensionHost
from pickel.extensions_host.registry import ExtensionRegistry
from pickel.tools.bus import ToolBus


def _host(section: dict | None) -> tuple[ExtensionHost, ExtensionRegistry]:
    registry = ExtensionRegistry()
    host = ExtensionHost(
        name="openviking",
        config_section=section,
        tool_bus=ToolBus(),
        registry=registry,
    )
    return host, registry


_MINIMAL = {
    "base_url": "https://ov.example",
    "account_id": "acct",
    "user_id": "user",
    "user_key": "key",
}


class OpenVikingSetupTests(unittest.TestCase):
    def test_registers_nothing_when_section_absent(self) -> None:
        host, registry = _host(None)

        setup(host)

        self.assertEqual([], registry.recall_factories)
        self.assertEqual([], registry.sync_factories)

    def test_registers_nothing_when_disabled(self) -> None:
        host, registry = _host({**_MINIMAL, "enabled": False})

        setup(host)

        self.assertEqual([], registry.recall_factories)
        self.assertEqual([], registry.sync_factories)

    def test_registers_sync_when_enabled(self) -> None:
        host, registry = _host({**_MINIMAL, "enabled": True})

        setup(host)

        self.assertEqual(1, len(registry.sync_factories))

    def test_recall_factory_registered_only_when_session_recall_enabled(self) -> None:
        host_off, registry_off = _host(
            {**_MINIMAL, "enabled": True, "session_recall": {"enabled": False}}
        )
        setup(host_off)

        host_on, registry_on = _host(
            {**_MINIMAL, "enabled": True, "session_recall": {"enabled": True}}
        )
        setup(host_on)

        self.assertEqual([], registry_off.recall_factories)
        self.assertEqual(1, len(registry_on.recall_factories))

    def test_factory_returns_none_for_agent_without_remote_id(self) -> None:
        host, registry = _host({**_MINIMAL, "enabled": True})
        setup(host)

        app_config = SimpleNamespace(
            default_agent="Pickle",
            agents={"Pickle": SimpleNamespace(remote_agent_id=None)},
        )
        scope = SimpleNamespace(agent_id="Pickle", app_config=app_config)

        self.assertIsNone(registry.sync_factories[0](scope))


if __name__ == "__main__":
    unittest.main()
```

最后一个用例的 `app_config` 形状要与实现里读取 `remote_agent_id` 的方式对齐 —— 先看现状 `boot._resolve_openviking_remote_agent_id` 的读法（先查 `openviking.agents.<id>`，再查 `app_config.get_agent_config(<id>).remote_agent_id`），实现时保持同样的优先级，测试的假 config 按实现调整。**不要改变原有优先级**，那是行为回归面。

- [x] **Step 2: 跑测试确认失败**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/extensions/openviking/test_setup.py -q
```

Expected: FAIL，`ImportError: cannot import name 'setup'`

- [x] **Step 3: 实现 `setup()`**

`src/pickel/extensions/openviking/__init__.py`。把 `boot.py` 的四个方法（`_build_recall_sources` / `_build_session_sync` / `_build_session_recall_provider` / `_resolve_openviking_remote_agent_id`）逐段搬过来，改写为模块级函数 + 两个工厂。骨架：

```python
"""OpenViking extension：会话同步与按 query 的记忆召回。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from pickel.config.paths import sessions_db_path
from pickel.extensions.openviking.bypass_store import OpenVikingBypassStore
from pickel.extensions.openviking.commit_policy import ThresholdCommitPolicy
from pickel.extensions.openviking.config import OpenVikingConfig
from pickel.extensions.openviking.context_client import SyncHTTPOpenVikingContextClient
from pickel.extensions.openviking.recall_adapter import OpenVikingRecall
from pickel.extensions.openviking.session_client import SyncHTTPOpenVikingSessionClient
from pickel.extensions.openviking.session_message_mapper import SessionMessageMapper
from pickel.extensions.openviking.session_recall import OpenVikingSessionRecallProvider
from pickel.extensions.openviking.session_sync import OpenVikingSessionSync


def setup(host) -> None:
    config = host.config(OpenVikingConfig)
    if config is None or not config.enabled:
        return

    host.add_session_sync(lambda scope: _make_session_sync(config, scope))
    if config.session_recall.enabled:
        host.add_recall_source(lambda scope: _make_recall(config, scope))


def _resolve_remote_agent_id(config: OpenVikingConfig, scope) -> str | None:
    """优先取 config.agents.<id>.remote_agent_id，回落到 agent 自己的配置字段。

    与迁移前 boot._resolve_openviking_remote_agent_id 的优先级保持一致。
    """
    remote_agent_config = config.agents.get(scope.agent_id)
    if remote_agent_config is not None:
        if not remote_agent_config.enabled:
            return None
        return remote_agent_config.remote_agent_id
    return scope.app_config.get_agent_config(scope.agent_id).remote_agent_id


def _make_session_sync(config: OpenVikingConfig, scope) -> Any | None:
    remote_agent_id = _resolve_remote_agent_id(config, scope)
    if remote_agent_id is None:
        return None
    db_path = sessions_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return OpenVikingSessionSync(
        config=config,
        remote_agent_id=remote_agent_id,
        client=SyncHTTPOpenVikingSessionClient(config, remote_agent_id=remote_agent_id),
        message_mapper=SessionMessageMapper(
            tool_output_max_chars=config.tool_output_max_chars
        ),
        commit_policy=ThresholdCommitPolicy(
            commit_after=timedelta(minutes=config.commit_after_minutes),
            commit_after_turns=config.commit_after_turns,
        ),
        state_store=OpenVikingBypassStore(db_path),
    )


def _make_recall(config: OpenVikingConfig, scope) -> Any | None:
    remote_agent_id = _resolve_remote_agent_id(config, scope)
    if remote_agent_id is None:
        return None
    db_path = sessions_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    provider = OpenVikingSessionRecallProvider(
        config=config,
        client=SyncHTTPOpenVikingContextClient(config, remote_agent_id=remote_agent_id),
        state_store=OpenVikingBypassStore(db_path),
    )
    return OpenVikingRecall(
        provider=provider,
        max_chars=config.session_recall.max_chars,
    )
```

**行为回归要点**：迁移前 `_resolve_openviking_remote_agent_id` 在「openviking 启用但 agent 无 `remote_agent_id`」时抛 `ValueError`；工厂路径改为返回 `None`（该 agent 不启用）。这是有意的行为变更 —— 一个 extension 配不全不该让 agent 起不来。在 `setup` 的 docstring 里写明。

- [x] **Step 4: 跑测试**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/extensions/ -q 2>&1 | tail -3
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
```

Expected: 新测试全过；全量失败清单不变（尚未接线，旧路径仍在跑）。

- [x] **Step 5: Commit**

```bash
git add src/pickel/extensions/openviking/__init__.py tests/extensions/openviking/test_setup.py
git commit -m "feat(extensions): openviking 的 setup 与两个 per-agent 工厂"
```

---

## Task 7c: 原子切换

**不可拆分提交** —— 拆开会双重注册（新旧两路都注册 sync）或功能中断。

**Files:**
- Modify: `src/pickel/app/boot.py`（删四个 `_build_*` openviking 方法与相关 import）
- Modify: `src/pickel/config/app_config.py`（删 `openviking` 字段与其 import）
- Modify: `src/pickel/config/loader.py`（删 openviking 专段）
- Modify: `config.yaml`（顶层 `openviking:` → `extensions.openviking:`）

- [x] **Step 1: 改 `config.yaml`**

把顶层 `openviking:` 整段改为 `extensions.openviking:`（缩进整体右移两格）：

```yaml
extensions:
  openviking:
    enabled: false
    base_url: ${OPENVIKING_BASE_URL}
    account_id: ${OPENVIKING_ACCOUNT_ID}
    user_id: ${OPENVIKING_USER_ID}
    user_key: ${OPENVIKING_USER_KEY}
    timeout_seconds: 30
    commit_after_minutes: 30
    commit_after_turns: 8
    tool_output_max_chars: 4000
    session_recall:
      enabled: false
      max_chars: 6000
      limit: 5
      min_score: null
```

- [x] **Step 2: 删 boot 的四个方法与并列路径**

`src/pickel/app/boot.py`：

- 删 `_build_recall_sources`、`_build_session_sync`、`_build_session_recall_provider`、`_resolve_openviking_remote_agent_id` 四个方法
- `build_run` 的 `recall_sources` 简化为 `self.resolve_recall_sources(agent.agent_id)`
- `build_session_service` 简化为 `SessionService(repository, CompositeSessionSync(self.resolve_session_syncs(agent_id)))`
- 删掉全部 openviking import 与 `SessionRecallProvider` / `NoopSessionRecallProvider` / `SessionSync` / `NoopSessionSync` / `timedelta` 等只被删除代码用到的 import

- [x] **Step 3: 删 `AppConfig.openviking`**

`src/pickel/config/app_config.py`：删 `openviking: OpenVikingConfig | None = None` 字段与 `from pickel.extensions.openviking.config import OpenVikingConfig`（Task 7a 后路径已变）。

- [x] **Step 4: 删 loader 的 openviking 专段**

`src/pickel/config/loader.py`：删掉 Task 2 Step 4 保留的那段（`openviking = merged.get("openviking")` 起、`merged.pop("openviking", None)` 止）。`extensions` 段的通用合并保留。

- [x] **Step 5: 解耦验收**

```bash
grep -rn "openviking" src/pickel/app/ src/pickel/config/ src/pickel/conversations/ src/pickel/context/ | grep -v __pycache__
```

Expected: **无命中**（`config/migrate.py` 的迁移逻辑是 Task 9 才加，此时也应无命中）。这是 E1 的核心判据。

- [x] **Step 6: 跑全量**

```bash
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -5
```

Expected: 全量失败清单不变。若 `tests/config/` 有断言 `AppConfig.openviking` 的用例，改为断言 `config.extensions["openviking"]`；若 `tests/app/` 有测 `_build_*` 方法的用例，删除（那些方法已不存在）。

```bash
grep -rn "\.openviking\b\|_build_session_sync\|_build_recall_sources" tests/ | grep -v __pycache__
```

- [x] **Step 7: 手动验收**

```bash
uv run pickel chat
```

确认能正常起对话（`extensions.openviking.enabled` 为 `false`，等于零注册）。退出。

- [x] **Step 8: Commit**

```bash
git add -A src/pickel config.yaml tests/
git commit -m "refactor(app): openviking 切换到 extension 路径，core 完成解耦

boot.py 的四个 _build_* 方法与 AppConfig.openviking 字段一并删除，
core 不再有任何 openviking 引用。"
```

---

## Task 8: CLI 装载入口 + `/reload` 重载

**Files:**
- Modify: `src/pickel/cli/main.py`（`_boot()`）
- Modify: `src/pickel/cli/chat.py`（`_handle_reload_command`、装载错误渲染）
- Test: `tests/cli/test_extension_load_errors.py`（新建）

- [x] **Step 1: `_boot()` 装载 extension**

`src/pickel/cli/main.py` 的 `_boot()`：

```python
def _boot() -> Boot:
    app_config = Config.load(cwd=Path.cwd())
    tool_bus = ToolBus()
    install_builtin_tools(tool_bus)
    result = load_extensions(tool_bus=tool_bus, app_config=app_config)
    for error in result.errors:
        typer.secho(f"Extension load error: {error}", fg=typer.colors.YELLOW, err=True)
    return Boot.from_config(app_config, tool_bus=tool_bus, extensions=result.registry)
```

顶部 import 增加 `ToolBus`、`install_builtin_tools`、`load_extensions`。**装载错误只警告、不阻止启动**。

- [x] **Step 2: `/reload` 重载 extension**

`src/pickel/cli/chat.py` 的 `_handle_reload_command`：在 `Boot.from_config(...)` 之前，先卸载再重装：

```python
            asyncio.run(
                teardown_extensions(self._extension_result, tool_bus=self._tool_bus)
            )
            self._extension_result = load_extensions(
                tool_bus=self._tool_bus,
                app_config=app_config,
            )
            for error in self._extension_result.errors:
                self._render_error_message(f"Extension load error: {error}")
            boot = Boot.from_config(
                app_config,
                tool_bus=self._tool_bus,
                extensions=self._extension_result.registry,
            )
```

`ChatApp.__init__` 存 `self._extension_result`（从 boot 传入或初始为空 `LoadResult()`）。`ChatApp` 需要能拿到 `LoadResult` —— 最简做法：`Boot` 上存一份（`Boot.extension_result`），`_boot()` 里赋值，`ChatApp` 从 `boot` 读。

若嫌在同步方法里 `asyncio.run` 别扭，给 `teardown_extensions` 加一个同步包装 `teardown_extensions_sync(result, *, tool_bus)`，内部 `asyncio.run`，与 `load_extensions` 的形态一致。

- [x] **Step 3: 写测试**

创建 `tests/cli/test_extension_load_errors.py`，测「装载失败只警告不抛」：

```python
"""装载失败不阻止启动。"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from pickel.extensions_host.loader import load_extensions
from pickel.tools.bus import ToolBus


class ExtensionLoadErrorTests(unittest.TestCase):
    def test_broken_extension_yields_error_without_raising(self) -> None:
        with TemporaryDirectory() as tmp:
            home = Path(tmp)
            ext_dir = home / "extensions" / "broken"
            ext_dir.mkdir(parents=True)
            (ext_dir / "__init__.py").write_text(
                "raise RuntimeError('boom')\n", encoding="utf-8"
            )

            result = load_extensions(
                tool_bus=ToolBus(),
                app_config=SimpleNamespace(extensions={}),
                home=home,
            )

            self.assertEqual(1, len(result.errors))
            self.assertEqual([], result.registry.extension_names)


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 4: 跑全量**

```bash
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
```

Expected: 全量失败清单不变。

- [x] **Step 5: 手动验收**

```bash
mkdir -p ~/.pickel/extensions/probe
cat > ~/.pickel/extensions/probe/__init__.py <<'PY'
from pickel.tools.base import BaseTool, ToolExecutionResult, ToolSpec


class PingTool(BaseTool):
    spec = ToolSpec(
        name="ping",
        description="Return pong. Probe tool from the probe extension.",
        input_schema={"type": "object", "properties": {}},
    )

    async def execute(self, arguments, context):
        return ToolExecutionResult(content="pong")


def setup(host):
    host.register_tool(PingTool())
PY
uv run pickel chat
```

在会话里：把 `ext__probe__ping` 加进 `agents/Pickle/agent.yaml` 的 `tools` 白名单后 `/reload`，让 agent 调它，应返回 `pong`。再故意把 `__init__.py` 改成 `raise RuntimeError('x')`，`/reload` 应只渲染一条警告、对话仍可用。验收完删掉探针：

```bash
rm -rf ~/.pickel/extensions/probe
```

- [x] **Step 6: Commit**

```bash
git add src/pickel/cli/ tests/cli/test_extension_load_errors.py
git commit -m "feat(cli): 启动与 /reload 装载 extension，错误只警告不阻断"
```

---

## Task 9: migrate 折算

**Files:**
- Modify: `src/pickel/config/migrate.py`
- Test: `tests/config/test_migrate.py`（追加；若无此文件则新建）

旧 `config.yaml` 的顶层 `openviking` 段要分两路折算：策略 → settings 的 `extensions.openviking`；密钥 → `auth.json` 的 `extensions.openviking`。

- [x] **Step 1: 写失败测试**

在 `tests/config/test_migrate.py` 的测试类里追加。该文件已有 `_write_yaml` 与 `_minimal_yaml` helper（见 line 93 附近用法），直接复用：

```python
    def test_legacy_openviking_section_migrates_under_extensions(self) -> None:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            home = root / "home"
            project = root / "project"
            project.mkdir()
            (project / "agents" / "Pickle").mkdir(parents=True)
            (project / "agents" / "Pickle" / "AGENT.md").write_text(
                "# Pickle\n", encoding="utf-8"
            )
            config_path = project / "config.yaml"
            raw = self._minimal_yaml()
            raw["openviking"] = {
                "enabled": True,
                "base_url": "https://ov.example",
                "account_id": "acct",
                "user_id": "user",
                "user_key": "secret",
                "commit_after_minutes": 15,
                "commit_after_turns": 4,
                "session_recall": {"enabled": True, "max_chars": 1234},
            }
            self._write_yaml(config_path, raw)

            with patch.dict(os.environ, {"PICKEL_HOME": str(home)}):
                migrate_from_yaml(config_path, home=home, project_root=project)

            settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
            auth = json.loads((home / "auth.json").read_text(encoding="utf-8"))

            # 策略进 settings.extensions.openviking，且不再有顶层 openviking 段
            strategy = settings["extensions"]["openviking"]
            self.assertTrue(strategy["enabled"])
            self.assertEqual(15, strategy["commit_after_minutes"])
            self.assertEqual(4, strategy["commit_after_turns"])
            self.assertEqual({"enabled": True, "max_chars": 1234}, strategy["session_recall"])
            self.assertNotIn("user_key", strategy)
            self.assertNotIn("openviking", settings)

            # 密钥进 auth.extensions.openviking，且不再有顶层 openviking 段
            secrets = auth["extensions"]["openviking"]
            self.assertEqual("secret", secrets["user_key"])
            self.assertEqual("https://ov.example", secrets["base_url"])
            self.assertEqual("acct", secrets["account_id"])
            self.assertEqual("user", secrets["user_id"])
            self.assertNotIn("commit_after_minutes", secrets)
            self.assertNotIn("openviking", auth)
```

顶部若缺 `import json` 则补上。策略/密钥的键划分由 `migrate.py` 顶部既有的 `_OPENVIKING_STRATEGY_KEYS`（`enabled` / `timeout_seconds` / `commit_after_minutes` / `commit_after_turns` / `tool_output_max_chars` / `session_recall` / `agents`）与 `_OPENVIKING_SECRET_KEYS`（`base_url` / `account_id` / `user_id` / `user_key`）决定 —— **不要重新划分**，只改它们的落点。

同时该文件已有的 `test_migrate_writes_settings_models_auth_and_agents` 若断言了 `settings["openviking"]` 或 `auth["openviking"]`，改为 `settings["extensions"]["openviking"]` / `auth["extensions"]["openviking"]`：

```bash
grep -n '"openviking"' tests/config/test_migrate.py
```

- [x] **Step 2: 实现**

`src/pickel/config/migrate.py`：
- `_build_settings` 里把 `settings["openviking"] = strategy` 改为 `settings.setdefault("extensions", {})["openviking"] = strategy`
- `_build_auth` 里把 `auth["openviking"] = ov_secrets` 改为 `auth.setdefault("extensions", {})["openviking"] = ov_secrets`

- [x] **Step 3: 跑测试**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/config/ -q 2>&1 | tail -3
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
```

Expected: 全量失败清单不变。

- [x] **Step 4: Commit**

```bash
git add src/pickel/config/migrate.py tests/config/test_migrate.py
git commit -m "feat(config): migrate 把旧 openviking 段折算进 extensions"
```

---

## Task 10: 验收与文档校对

- [x] **Step 1: 解耦判据**

```bash
grep -rn "openviking" src/pickel/app/ src/pickel/config/app_config.py src/pickel/config/loader.py \
       src/pickel/conversations/ src/pickel/context/ src/pickel/runs/ | grep -v __pycache__
```

Expected: 无命中。`config/migrate.py` 允许命中（迁移逻辑必须认识旧名字）。

```bash
wc -l src/pickel/app/boot.py
```

Expected: 约 110 行（迁移前 220 行）。

- [x] **Step 2: 全量测试**

```bash
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | grep FAILED | sort
```

Expected: 失败清单只有 `tests/tools/test_shell.py`（6 例，属 S1）与 `tests/providers/`（12 例，缺 key）。

- [x] **Step 3: 手动验收清单**

```bash
uv run pickel chat
```

- 起对话、让 agent 读文件、跑 shell 命令 —— 核心路径不受影响
- `/reload` 正常
- `pickel sessions` 列表正常（`build_session_service` 已换 `CompositeSessionSync`）

- [x] **Step 4: 校对设计稿**

按实际实现修订 `docs/upgrade/2026-07-26-extension-host-design.md`：
- §3.1 若 `ExtensionHost` 的构造参数与稿中不同，据实修改
- §5 补 `LoadResult` 的实际形状（`registry` / `errors` / `modules`）
- §7 补「openviking 配不全时从抛 `ValueError` 改为返回 `None`」这条行为变更（Task 7b Step 3）
- §10 补实施中发现的新取舍

- [x] **Step 5: Commit**

```bash
git add -A docs/upgrade/2026-07-26-extension-host-design.md
git commit -m "docs(extensions): E1 设计稿按实现校对"
```

---

## 完成标准

1. `grep -rn "openviking" src/pickel/app/ src/pickel/config/app_config.py src/pickel/config/loader.py` 无命中。
2. `src/pickel/app/boot.py` 约 110 行，无任何 openviking 代码。
3. 全量测试失败清单与 T1 完成时逐条相同。
4. 用户级探针 extension 能注册工具、被 agent 调用；坏 extension 只警告不阻断启动。
5. `LifecycleHooks` 在生产路径接上（`build_run` 传入非 Noop 实例）。
6. 设计稿与实现一致（Task 10 Step 4 已校对）。

## 已知不在本计划内

| 项 | 归属 |
| --- | --- |
| `register_command`（`chat.py` if/elif 链重构）、`add_skill_path`、`register_provider`、compaction hook、`input` 拦截、UI 渲染器 | E2 |
| 项目级 `.pickel/extensions/` 与信任门 | E2 |
| 运行时动态注册（装载后再 `register_tool` 立即生效） | E2 |
| MCP 客户端（作为内置 extension 实现） | T2 |
| extension 的版本、依赖约束、打包分发 | V1 |
| extension 代码本身的沙箱化 | S2 |
