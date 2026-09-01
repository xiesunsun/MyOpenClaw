# T1 工具总线与热插拔 — 实施计划

> **For agentic workers:** 按任务顺序实现，步骤用 checkbox 跟踪。设计依据：`docs/upgrade/2026-07-26-tool-bus-design.md`；调研依据：`docs/upgrade/2026-07-26-tools-sandbox-research.md`。

**Goal:** 把静态 `ToolRegistry` 改造为进程级可变 `ToolBus`，注册表与激活集分离，工具集在 turn 边界快照，为 E1（extension 宿主）、T2（MCP 客户端）与 V1（版本管理）留好挂点。

**Architecture:** 三层分离 —— `ToolBus`（进程级注册表，随时可变）／`ToolActivation`（turn 级激活集，三层求交）／`ToolSnapshot`（turn 内不可变视图，prepare 与 react 共用同一份）。`ToolExecutionContext` 的 `Any` 字段收敛为强类型 `ToolServices`。

**Tech Stack:** Python 3.12、dataclasses、StrEnum、unittest（pytest 运行）。

## Global Constraints

- 每个任务结束时测试基线不得退化。**基线（T1 开始前实测）：`18 failed, 331 passed, 1 skipped`**。判据是**失败数恒为 18 且按文件的失败分布不变**，通过数只增不减。构成：6 例 `tests/tools/test_shell.py`（Linux bash bracketed-paste 转义，属 S1）；12 例缺 API key 的 provider 初始化失败，分散在 `tests/app/test_assembly.py`(3)、`tests/cli/test_chat_loop.py`(1)、`tests/providers/test_gemini.py`(7)、`tests/providers/test_model_context_generate.py`(1)。核对用：
  ```bash
  uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | grep FAILED | sed 's/::.*//' | sort | uniq -c
  ```
- 测试命令：`uv run --with pytest --with pytest-asyncio pytest -q`（本仓库 `pyproject.toml` 未声明 pytest 依赖，必须用 `--with`）。
- 新模块首行 `from __future__ import annotations`，与 `src/pickel/tools/` 现有模块一致。
- 注释与 docstring 用中文，命名直接通俗（`AGENTS.md` 编码原则）。
- `tests/tools/` 用 `unittest.TestCase` / `unittest.IsolatedAsyncioTestCase` 类风格；`tests/context/test_prepare.py` 是 pytest 函数式 —— 改到哪个文件就follow该文件的既有风格。
- **工具的唯一标识是 `ToolEntry.name`（含命名空间前缀），不是 `tool.spec.name`。** 白名单、给模型的 `ToolDefinition.name`、模型回传的 `tool_call.name`、快照查找键，全部用 `ToolEntry.name`。内置工具二者恰好相等，MCP / extension 工具不等。
- 不改 `config.yaml` 与 `agents/*/agent.yaml` 的 `tools` 白名单语义（零迁移）。

---

## 文件地图（目标）

| 路径 | 职责 | 状态 |
| --- | --- | --- |
| `src/pickel/tools/bus.py` | `ToolSource` / `ToolEntry` / `ToolActivation` / `ToolSnapshot` / `ToolBus` / `ToolNameConflictError` | 新建 |
| `src/pickel/tools/services.py` | `ToolServices`：进程内工具可用的运行期服务 | 新建 |
| `src/pickel/tools/registry.py` | `ToolRegistry` | **Task 9 删除** |
| `src/pickel/tools/base.py` | `ToolExecutionContext.services` 取代两个 `Any` 字段 | 改 |
| `src/pickel/tools/catalog.py` | 增 `install_builtin_tools(bus)` | 改 |
| `src/pickel/tools/builtin.py` | 增 `tool_set_active` | 改 |
| `src/pickel/tools/file_tools.py` | `_require_workspace_files` 改读 `context.services` | 改 |
| `src/pickel/tools/shell.py` | `_require_shell_manager` 改读 `context.services` | 改 |
| `src/pickel/runs/run.py` | 持 `tool_bus` + `activation`；`open()` 接受注入；组装 `ToolServices` | 改 |
| `src/pickel/runs/turn_state.py` | `TurnState.tool_snapshot` | 改 |
| `src/pickel/runs/strategy/react.py` | turn 开始取快照；执行与 prepare 都用快照 | 改 |
| `src/pickel/context/prepare.py` | `resolve_tools(*, snapshot)` | 改 |
| `src/pickel/app/boot.py` | 接受并透传 `tool_bus` | 改 |
| `src/pickel/cli/chat.py` | 持有进程级 bus，`/reload` 复用 | 改 |
| `tests/tools/test_bus.py` | bus 与激活集、快照测试 | 新建 |
| `tests/tools/test_registry.py` | | **Task 9 删除** |

## 任务顺序与依赖

```text
Task 1  ToolBus 注册与来源分层          （无依赖）
Task 2  激活集与快照                    （依赖 1）
Task 3  ToolServices 强类型化            （无依赖，可与 1/2 并行）
Task 4  Run 接入 bus                    （依赖 1、2、3）
Task 5  TurnState 快照 + react 执行改造  （依赖 4）
Task 6  prepare 改用快照                （依赖 5）
Task 7  catalog + Boot + ChatApp 注入    （依赖 4）
Task 8  tool_set_active 内置工具         （依赖 5、7）
Task 9  删除 ToolRegistry 与兼容层       （依赖全部）
```

顺序经过排布，保证**每个任务结束时全量测试都不退化** —— Task 4 保留 `Run.tools` 兼容 property，直到 Task 9 才删。

---

## Task 1: ToolBus 注册与来源分层

**Files:**
- Create: `src/pickel/tools/bus.py`
- Test: `tests/tools/test_bus.py`

**Interfaces:**
- Consumes: `pickel.tools.base.BaseTool`、`ToolSpec`
- Produces:
  - `ToolSource(StrEnum)`：`BUILTIN = "builtin"` / `MCP = "mcp"` / `EXTENSION = "extension"`
  - `ToolNameConflictError(Exception)`
  - `ToolEntry(name: str, tool: BaseTool, source: ToolSource, version: str | None, origin: str | None, enabled: bool)` — frozen dataclass
  - `ToolBus.register(tool: BaseTool, *, source: ToolSource, version: str | None = None, origin: str | None = None) -> str`（返回最终名）
  - `ToolBus.unregister(name: str) -> None`
  - `ToolBus.unregister_origin(source: ToolSource, origin: str) -> list[str]`
  - `ToolBus.set_enabled(name: str, enabled: bool) -> None`
  - `ToolBus.get(name: str) -> ToolEntry`（未知名抛 `KeyError`）
  - `ToolBus.list(*, source: ToolSource | None = None) -> list[ToolEntry]`
  - `ToolBus.list_names(*, source: ToolSource | None = None) -> list[str]`
  - `bus_with(tools: Iterable[BaseTool]) -> ToolBus`（模块级函数）—— 用一组工具建一个私有 bus，全部登记为 `BUILTIN`。`Run.open` 的 `tools=` 便捷路径与直接构造 `Run(...)` 的测试共用它，避免同一段样板重复五遍。Task 2 之后配合 `ToolActivation(allowed=frozenset(bus.list_names()))` 使用。

- [x] **Step 1: 写失败测试**

创建 `tests/tools/test_bus.py`：

```python
import unittest

from pickel.tools.base import BaseTool, ToolSpec
from pickel.tools.bus import (
    ToolBus,
    ToolNameConflictError,
    ToolSource,
)


def _stub_tool(name: str) -> BaseTool:
    """造一个只有 spec 的最小工具，够 bus 测试用。"""

    class _Stub(BaseTool):
        spec = ToolSpec(
            name=name,
            description=f"{name} description",
            input_schema={"type": "object", "properties": {}},
        )

    return _Stub()


class ToolBusRegistrationTests(unittest.TestCase):
    def test_builtin_tool_keeps_bare_name(self) -> None:
        bus = ToolBus()

        name = bus.register(_stub_tool("read_file"), source=ToolSource.BUILTIN)

        self.assertEqual("read_file", name)
        self.assertEqual(["read_file"], bus.list_names())

    def test_mcp_tool_gets_namespace_prefix_but_spec_stays_bare(self) -> None:
        bus = ToolBus()

        name = bus.register(
            _stub_tool("create_issue"),
            source=ToolSource.MCP,
            origin="github",
        )

        self.assertEqual("mcp__github__create_issue", name)
        entry = bus.get("mcp__github__create_issue")
        self.assertEqual("mcp__github__create_issue", entry.name)
        self.assertEqual("create_issue", entry.tool.spec.name)

    def test_extension_tool_uses_its_own_prefix(self) -> None:
        bus = ToolBus()

        name = bus.register(
            _stub_tool("recall_search"),
            source=ToolSource.EXTENSION,
            origin="openviking",
        )

        # extension 工具跑在本进程内，与 MCP 的子进程工具前缀必须分开
        self.assertEqual("ext__openviking__recall_search", name)
        self.assertEqual(ToolSource.EXTENSION, bus.get(name).source)

    def test_same_origin_across_mcp_and_extension_does_not_collide(self) -> None:
        bus = ToolBus()

        mcp_name = bus.register(_stub_tool("run"), source=ToolSource.MCP, origin="shared")
        ext_name = bus.register(
            _stub_tool("run"), source=ToolSource.EXTENSION, origin="shared"
        )

        self.assertEqual("mcp__shared__run", mcp_name)
        self.assertEqual("ext__shared__run", ext_name)
        self.assertEqual(2, len(bus.list_names()))

    def test_non_builtin_without_origin_is_rejected(self) -> None:
        bus = ToolBus()

        with self.assertRaises(ValueError):
            bus.register(_stub_tool("create_issue"), source=ToolSource.MCP)

    def test_same_source_and_origin_overwrites_and_keeps_enabled_flag(self) -> None:
        bus = ToolBus()
        name = bus.register(_stub_tool("create_issue"), source=ToolSource.MCP, origin="github")
        bus.set_enabled(name, False)

        replacement = _stub_tool("create_issue")
        bus.register(replacement, source=ToolSource.MCP, origin="github", version="v2")

        entry = bus.get(name)
        self.assertIs(replacement, entry.tool)
        self.assertEqual("v2", entry.version)
        self.assertFalse(entry.enabled)  # 运维关掉的工具不因重连自动打开

    def test_origin_containing_double_underscore_is_rejected(self) -> None:
        # 否则 server "a" + 工具 "b__c" 与 server "a__b" + 工具 "c"
        # 会得到同一个 mcp__a__b__c，前缀方案出现歧义
        bus = ToolBus()

        with self.assertRaises(ValueError):
            bus.register(_stub_tool("c"), source=ToolSource.MCP, origin="a__b")

    def test_builtin_name_shaped_like_a_qualified_name_conflicts(self) -> None:
        # 唯一还能触发跨来源撞名的场景：内置工具名恰好长成前缀形式
        bus = ToolBus()
        bus.register(_stub_tool("mcp__github__create_issue"), source=ToolSource.BUILTIN)

        with self.assertRaises(ToolNameConflictError):
            bus.register(
                _stub_tool("create_issue"), source=ToolSource.MCP, origin="github"
            )

    def test_builtin_and_prefixed_tool_never_collide(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("read_file"), source=ToolSource.BUILTIN)
        bus.register(_stub_tool("read_file"), source=ToolSource.MCP, origin="remote")

        self.assertEqual(
            ["mcp__remote__read_file", "read_file"],
            sorted(bus.list_names()),
        )

    def test_unknown_name_raises_key_error(self) -> None:
        bus = ToolBus()

        with self.assertRaises(KeyError):
            bus.get("missing_tool")


class ToolBusLifecycleTests(unittest.TestCase):
    def test_unregister_removes_single_entry(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("echo"), source=ToolSource.BUILTIN)

        bus.unregister("echo")

        self.assertEqual([], bus.list_names())

    def test_unregister_origin_removes_all_of_that_server(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("echo"), source=ToolSource.BUILTIN)
        bus.register(_stub_tool("a"), source=ToolSource.MCP, origin="github")
        bus.register(_stub_tool("b"), source=ToolSource.MCP, origin="github")
        bus.register(_stub_tool("c"), source=ToolSource.MCP, origin="slack")

        removed = bus.unregister_origin(ToolSource.MCP, "github")

        self.assertEqual(["mcp__github__a", "mcp__github__b"], sorted(removed))
        self.assertEqual(["echo", "mcp__slack__c"], sorted(bus.list_names()))

    def test_list_filters_by_source(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("echo"), source=ToolSource.BUILTIN)
        bus.register(_stub_tool("a"), source=ToolSource.MCP, origin="github")

        self.assertEqual(["echo"], bus.list_names(source=ToolSource.BUILTIN))
        self.assertEqual(["mcp__github__a"], bus.list_names(source=ToolSource.MCP))

    def test_set_enabled_toggles_without_removing(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("echo"), source=ToolSource.BUILTIN)

        bus.set_enabled("echo", False)

        self.assertFalse(bus.get("echo").enabled)
        self.assertEqual(["echo"], bus.list_names())

    def test_set_enabled_on_unknown_name_raises(self) -> None:
        bus = ToolBus()

        with self.assertRaises(KeyError):
            bus.set_enabled("missing_tool", False)


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: 跑测试确认失败**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/tools/test_bus.py -q
```

Expected: 全部 ERROR / FAIL，`ModuleNotFoundError: No module named 'pickel.tools.bus'`

- [x] **Step 3: 实现 `src/pickel/tools/bus.py`**

```python
"""工具总线：进程级注册表 + turn 级激活集与快照。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from pickel.tools.base import BaseTool


class ToolSource(StrEnum):
    """工具来源。三者命名空间前缀各自独立：builtin 裸名、mcp__、ext__。

    MCP 工具跑在子进程，extension 工具跑在本进程内 —— 执行位置与信任级别不同。
    """

    BUILTIN = "builtin"
    MCP = "mcp"
    EXTENSION = "extension"


class ToolNameConflictError(Exception):
    """不同来源注册了同名工具。命名空间前缀本应避免，撞了就是 bug，不静默覆盖。"""


@dataclass(frozen=True)
class ToolEntry:
    """总线中的一条工具记录。name 是含命名空间前缀的最终名，全总线唯一。"""

    name: str
    tool: BaseTool
    source: ToolSource
    version: str | None = None
    origin: str | None = None
    enabled: bool = True


_PREFIX_BY_SOURCE = {
    ToolSource.MCP: "mcp",
    ToolSource.EXTENSION: "ext",
}


def qualified_name(spec_name: str, source: ToolSource, origin: str | None) -> str:
    """按来源计算最终名。

    内置工具用裸名；MCP 工具加 mcp__<server>__，extension 工具加 ext__<extension>__。
    两者前缀不同：MCP 工具跑在子进程里，extension 工具跑在本进程内，
    执行位置与信任级别都不同，名字上就要能区分。
    """
    if source is ToolSource.BUILTIN:
        return spec_name
    if not origin:
        raise ValueError(f"source '{source}' requires a non-empty origin")
    if "__" in origin:
        # 否则 origin 'a' + 工具 'b__c' 与 origin 'a__b' + 工具 'c' 会撞成同一个名字
        raise ValueError(f"origin '{origin}' must not contain '__'")
    return f"{_PREFIX_BY_SOURCE[source]}__{origin}__{spec_name}"


class ToolBus:
    """进程级工具注册表。可变，跨 Run / session / reload 存活。"""

    def __init__(self) -> None:
        self._entries: dict[str, ToolEntry] = {}

    def register(
        self,
        tool: BaseTool,
        *,
        source: ToolSource,
        version: str | None = None,
        origin: str | None = None,
    ) -> str:
        """注册工具，返回最终名。同来源同 origin 视为重新注册（保留 enabled）。"""
        name = qualified_name(tool.spec.name, source, origin)
        existing = self._entries.get(name)
        if existing is not None and (existing.source is not source or existing.origin != origin):
            raise ToolNameConflictError(
                f"Tool '{name}' already registered by source '{existing.source}'"
            )
        self._entries[name] = ToolEntry(
            name=name,
            tool=tool,
            source=source,
            version=version,
            origin=origin,
            enabled=existing.enabled if existing is not None else True,
        )
        return name

    def unregister(self, name: str) -> None:
        self._entries.pop(name, None)

    def unregister_origin(self, source: ToolSource, origin: str) -> list[str]:
        """卸掉某来源某 origin 的全部工具，返回被卸的名字。

        E1 用于 extension 重载/卸载，T2 用于 MCP server 断开。
        """
        names = [
            name
            for name, entry in self._entries.items()
            if entry.source is source and entry.origin == origin
        ]
        for name in names:
            del self._entries[name]
        return names

    def set_enabled(self, name: str, enabled: bool) -> None:
        entry = self.get(name)
        self._entries[name] = replace(entry, enabled=enabled)

    def get(self, name: str) -> ToolEntry:
        try:
            return self._entries[name]
        except KeyError as exc:
            raise KeyError(f"Unknown tool: {name}") from exc

    def list(self, *, source: ToolSource | None = None) -> list[ToolEntry]:
        entries = list(self._entries.values())
        if source is None:
            return entries
        return [entry for entry in entries if entry.source is source]

    def list_names(self, *, source: ToolSource | None = None) -> list[str]:
        return [entry.name for entry in self.list(source=source)]


def bus_with(tools: Iterable[BaseTool]) -> ToolBus:
    """用一组工具建一个私有总线，全部登记为内置来源。

    供 Run.open 的 tools= 便捷路径与直接构造 Run 的测试共用。
    """
    bus = ToolBus()
    for candidate in tools:
        bus.register(candidate, source=ToolSource.BUILTIN)
    return bus
```

顶部 import 增加 `from collections.abc import Iterable`（Task 2 也需要它）。

- [x] **Step 4: 跑测试确认通过**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/tools/test_bus.py -q
```

Expected: PASS（全部通过）

- [x] **Step 5: 确认全量不退化**

```bash
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
```

Expected: `18 failed, <N> passed, 1 skipped` —— **失败数必须仍是 18**，通过数只增不减

- [x] **Step 6: Commit**

```bash
git add src/pickel/tools/bus.py tests/tools/test_bus.py
git commit -m "feat(tools): ToolBus 注册表与来源分层"
```

---

## Task 2: 激活集与快照

**Files:**
- Modify: `src/pickel/tools/bus.py`（追加 `ToolActivation`、`ToolSnapshot`、`ToolBus.snapshot`）
- Test: `tests/tools/test_bus.py`（追加两个测试类）

**Interfaces:**
- Consumes: Task 1 的 `ToolBus`、`ToolEntry`、`ToolSource`
- Produces:
  - `ToolActivation(allowed: frozenset[str], agent_disabled: frozenset[str])` — frozen dataclass
  - `ToolActivation.with_agent_disabled(names: Iterable[str]) -> ToolActivation`（并集，返回新实例）
  - `ToolActivation.with_agent_enabled(names: Iterable[str]) -> ToolActivation`（差集，返回新实例）
  - `ToolSnapshot(entries: tuple[ToolEntry, ...])` — frozen dataclass
  - `ToolSnapshot.find(name: str) -> BaseTool | None`
  - `ToolSnapshot.names -> tuple[str, ...]`（property）
  - `ToolBus.snapshot(activation: ToolActivation) -> ToolSnapshot`
  - `ToolBus.missing_names(activation: ToolActivation) -> list[str]`（白名单里 bus 没有的名字，供调用方记 warning）

- [x] **Step 1: 写失败测试**

追加到 `tests/tools/test_bus.py`（`ToolBusLifecycleTests` 之后、`if __name__` 之前）：

```python
class ToolActivationTests(unittest.TestCase):
    def test_snapshot_intersects_allowlist_enabled_and_agent_disabled(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("read_file"), source=ToolSource.BUILTIN)
        bus.register(_stub_tool("write_file"), source=ToolSource.BUILTIN)
        bus.register(_stub_tool("shell_exec"), source=ToolSource.BUILTIN)
        bus.register(_stub_tool("echo"), source=ToolSource.BUILTIN)
        bus.set_enabled("write_file", False)

        activation = ToolActivation(
            allowed=frozenset({"read_file", "write_file", "shell_exec"}),
            agent_disabled=frozenset({"shell_exec"}),
        )
        snapshot = bus.snapshot(activation)

        # write_file 被 bus 禁用、shell_exec 被 agent 关闭、echo 不在白名单
        self.assertEqual(("read_file",), snapshot.names)

    def test_agent_cannot_widen_beyond_allowlist(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("read_file"), source=ToolSource.BUILTIN)
        bus.register(_stub_tool("shell_exec"), source=ToolSource.BUILTIN)

        activation = ToolActivation(allowed=frozenset({"read_file"}))
        # agent 把白名单外的工具从 disabled 里移出，也拉不进来
        activation = activation.with_agent_enabled(["shell_exec"])

        self.assertEqual(("read_file",), bus.snapshot(activation).names)

    def test_with_agent_disabled_returns_new_instance(self) -> None:
        activation = ToolActivation(allowed=frozenset({"a", "b"}))

        narrowed = activation.with_agent_disabled(["b"])

        self.assertEqual(frozenset(), activation.agent_disabled)
        self.assertEqual(frozenset({"b"}), narrowed.agent_disabled)

    def test_allowlist_entry_missing_from_bus_is_skipped_not_raised(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("read_file"), source=ToolSource.BUILTIN)

        activation = ToolActivation(
            allowed=frozenset({"read_file", "mcp__github__create_issue"})
        )

        self.assertEqual(("read_file",), bus.snapshot(activation).names)
        self.assertEqual(["mcp__github__create_issue"], bus.missing_names(activation))


class ToolSnapshotTests(unittest.TestCase):
    def test_snapshot_is_immune_to_later_bus_changes(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("read_file"), source=ToolSource.BUILTIN)
        activation = ToolActivation(allowed=frozenset({"read_file", "write_file"}))
        snapshot = bus.snapshot(activation)

        bus.register(_stub_tool("write_file"), source=ToolSource.BUILTIN)
        bus.set_enabled("read_file", False)
        bus.unregister("read_file")

        # 快照在 turn 内不可变：既看不到新注册的，也不受禁用与卸载影响
        self.assertEqual(("read_file",), snapshot.names)
        self.assertIsNotNone(snapshot.find("read_file"))

    def test_find_returns_none_for_unknown_name(self) -> None:
        bus = ToolBus()
        snapshot = bus.snapshot(ToolActivation(allowed=frozenset()))

        self.assertIsNone(snapshot.find("read_file"))

    def test_find_uses_entry_name_not_spec_name(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("create_issue"), source=ToolSource.MCP, origin="github")
        activation = ToolActivation(allowed=frozenset({"mcp__github__create_issue"}))

        snapshot = bus.snapshot(activation)

        self.assertIsNotNone(snapshot.find("mcp__github__create_issue"))
        self.assertIsNone(snapshot.find("create_issue"))
```

同时把文件顶部 import 补上：

```python
from pickel.tools.bus import (
    ToolActivation,
    ToolBus,
    ToolNameConflictError,
    ToolSnapshot,
    ToolSource,
)
```

- [x] **Step 2: 跑测试确认失败**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/tools/test_bus.py -q
```

Expected: FAIL，`ImportError: cannot import name 'ToolActivation'`

- [x] **Step 3: 实现**

在 `ToolEntry` 之后、`qualified_name` 之前插入（`Iterable` 已在 Task 1 导入）：

```python
@dataclass(frozen=True)
class ToolActivation:
    """一次 turn 的激活集计算输入。

    allowed 是 agent.yaml 的 tools 白名单，人的授权、硬边界；
    agent_disabled 是 agent 通过 tool_set_active 自我收窄的部分。
    求交顺序保证 agent 只能收窄、永不扩张。
    """

    allowed: frozenset[str]
    agent_disabled: frozenset[str] = frozenset()

    def with_agent_disabled(self, names: Iterable[str]) -> ToolActivation:
        return replace(self, agent_disabled=self.agent_disabled | frozenset(names))

    def with_agent_enabled(self, names: Iterable[str]) -> ToolActivation:
        return replace(self, agent_disabled=self.agent_disabled - frozenset(names))


@dataclass(frozen=True)
class ToolSnapshot:
    """turn 内不可变的工具视图。prepare 与 react 的唯一来源。

    不提供 definitions()：ToolDefinition 属 context 层，转换留在 prepare.resolve_tools，
    避免 tools 层反向依赖 context 层。
    """

    entries: tuple[ToolEntry, ...]

    def find(self, name: str) -> BaseTool | None:
        for entry in self.entries:
            if entry.name == name:
                return entry.tool
        return None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(entry.name for entry in self.entries)
```

在 `ToolBus` 类内追加：

```python
    def snapshot(self, activation: ToolActivation) -> ToolSnapshot:
        """按激活集三层求交，取本 turn 的不可变视图。"""
        entries = tuple(
            entry
            for entry in self._entries.values()
            if entry.enabled
            and entry.name in activation.allowed
            and entry.name not in activation.agent_disabled
        )
        return ToolSnapshot(entries=entries)

    def missing_names(self, activation: ToolActivation) -> list[str]:
        """白名单里存在、bus 中却没有的名字。调用方据此记 warning，不视为错误。"""
        return sorted(name for name in activation.allowed if name not in self._entries)
```

- [x] **Step 4: 跑测试确认通过**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/tools/test_bus.py -q
```

Expected: PASS（全部通过）

- [x] **Step 5: 确认全量不退化**

```bash
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
```

Expected: `18 failed, <N> passed, 1 skipped` —— **失败数必须仍是 18**，通过数只增不减

- [x] **Step 6: Commit**

```bash
git add src/pickel/tools/bus.py tests/tools/test_bus.py
git commit -m "feat(tools): 激活集三层求交与 turn 快照"
```

---

## Task 3: ToolServices 强类型化

把 `ToolExecutionContext` 的两个 `Any` 字段收敛为一个字段明确的容器。这是 S2 沙箱替换工具运行期能力的唯一入口。

**Files:**
- Create: `src/pickel/tools/services.py`
- Modify: `src/pickel/tools/base.py`（`ToolExecutionContext`）
- Modify: `src/pickel/tools/file_tools.py:12-14`
- Modify: `src/pickel/tools/shell.py:455`
- Modify: `src/pickel/runs/run.py`（`get_tool_execution_context`）
- Test: `tests/tools/test_base.py`、`tests/tools/test_filesystem.py`、`tests/tools/test_shell.py`（回归改造）

**Interfaces:**
- Produces:
  - `ToolServices(workspace_files: WorkspaceFileService | None = None, shell_sessions: ShellSessionManager | None = None)` — frozen dataclass
  - `ToolExecutionContext(agent_id: str, session_id: str, workspace_path: Path, services: ToolServices)`，`services` 默认 `ToolServices()`

- [x] **Step 1: 写失败测试**

在 `tests/tools/test_base.py` 末尾（`if __name__` 之前）追加：

```python
class ToolServicesTests(unittest.TestCase):
    def test_context_defaults_to_empty_services(self) -> None:
        from pickel.tools.services import ToolServices

        context = ToolExecutionContext(
            agent_id="Pickle",
            session_id="session-1",
            workspace_path=Path("/tmp/pickle"),
        )

        self.assertIsInstance(context.services, ToolServices)
        self.assertIsNone(context.services.workspace_files)
        self.assertIsNone(context.services.shell_sessions)

    def test_services_carries_injected_dependencies(self) -> None:
        from pickel.tools.services import ToolServices

        services = ToolServices(workspace_files="fake-files", shell_sessions="fake-shell")
        context = ToolExecutionContext(
            agent_id="Pickle",
            session_id="session-1",
            workspace_path=Path("/tmp/pickle"),
            services=services,
        )

        self.assertEqual("fake-files", context.services.workspace_files)
        self.assertEqual("fake-shell", context.services.shell_sessions)
```

- [x] **Step 2: 跑测试确认失败**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/tools/test_base.py -q
```

Expected: FAIL，`ModuleNotFoundError: No module named 'pickel.tools.services'`

- [x] **Step 3: 实现 `src/pickel/tools/services.py`**

```python
"""工具运行期服务容器。

宿主提供给进程内工具的服务。extension 工具也在进程内跑，但它在装载时
用闭包持有自己的依赖，只从这里取宿主服务；服务种类由 core 决定、数量有限，
一个字段明确的 dataclass 就够，不做「能力声明 + 按需注入」。
S2 沙箱化时从这里替换实现即可，工具侧代码不动。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 运行期不导入，避免 base ↔ shell / file_service 循环
    from pickel.tools.file_service import WorkspaceFileService
    from pickel.tools.shell import ShellSessionManager


@dataclass(frozen=True)
class ToolServices:
    workspace_files: "WorkspaceFileService | None" = None
    shell_sessions: "ShellSessionManager | None" = None
```

- [x] **Step 4: 改 `ToolExecutionContext`**

`src/pickel/tools/base.py`，把

```python
@dataclass(frozen=True)
class ToolExecutionContext:
    agent_id: str
    session_id: str
    workspace_path: Path
    workspace_files: Any = None
    shell_session_manager: Any = None
```

换成

```python
@dataclass(frozen=True)
class ToolExecutionContext:
    agent_id: str
    session_id: str
    workspace_path: Path
    services: ToolServices = field(default_factory=ToolServices)
```

顶部 import 增加 `from pickel.tools.services import ToolServices`（`services.py` 不导入 `base`，无循环）。若 `Any` 因此不再被使用，从 `typing` import 里去掉。

- [x] **Step 5: 改两个 helper**

`src/pickel/tools/file_tools.py` 的 `_require_workspace_files`：

```python
    if context.services.workspace_files is None:
        raise RuntimeError("A workspace file service is required for file tools")
    return context.services.workspace_files
```

`src/pickel/tools/shell.py` 的 `_require_shell_manager`：

```python
def _require_shell_manager(context: ToolExecutionContext) -> ShellSessionManager:
    manager = context.services.shell_sessions
    if manager is None:
        raise RuntimeError("A shell session manager is required for shell tools")
    return manager
```

- [x] **Step 6: 改 `Run.get_tool_execution_context`**

`src/pickel/runs/run.py`：

```python
    def get_tool_execution_context(self, session_id: str) -> ToolExecutionContext:
        return ToolExecutionContext(
            agent_id=self.agent.agent_id,
            session_id=session_id,
            workspace_path=self.agent.workspace,
            services=ToolServices(
                workspace_files=self.workspace_files,
                shell_sessions=self.shell_session_manager,
            ),
        )
```

顶部 import 增加 `from pickel.tools.services import ToolServices`。

- [x] **Step 7: 回归改造调用点**

找出全部旧关键字用法：

```bash
grep -rn "workspace_files=\|shell_session_manager=" src/ tests/ | grep -v __pycache__
```

对每一处 `ToolExecutionContext(...)` 构造，把

```python
                    workspace_files=<X>,
                    shell_session_manager=<Y>,
```

替换为

```python
                    services=ToolServices(workspace_files=<X>, shell_sessions=<Y>),
```

并在该测试文件顶部加 `from pickel.tools.services import ToolServices`。`<X>` / `<Y>` 为 `None` 时可整段省略（有默认值）。注意 `WorkspaceFileService(workspace_root=..., access_policy=...)` 自身的 `workspace_root=` 关键字不要误改。

- [x] **Step 8: 跑测试确认通过**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/tools/ -q 2>&1 | tail -5
```

Expected: 只剩 `tests/tools/test_shell.py` 原有 6 例 ANSI 失败，其余通过。

```bash
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
```

Expected: `18 failed, <N> passed, 1 skipped` —— **失败数必须仍是 18**，通过数只增不减

- [x] **Step 9: Commit**

```bash
git add src/pickel/tools/services.py src/pickel/tools/base.py src/pickel/tools/file_tools.py \
        src/pickel/tools/shell.py src/pickel/runs/run.py tests/tools/
git commit -m "refactor(tools): ToolExecutionContext 的 Any 字段收敛为 ToolServices"
```

---

## Task 4: Run 接入 bus

**Files:**
- Modify: `src/pickel/runs/run.py`
- Test: `tests/runs/test_runner.py`（追加测试类 + 改 `_run` helper）
- Modify: `tests/runs/test_react_checkpoint.py:85-88`、`tests/hooks/test_lifecycle_hooks.py:228-231`（直接构造 `Run(...)` 并传 `tools=` 的位置）

**注意：** `Run` 是 dataclass，`tests/runs/test_runner.py:88` 的 `_run` helper、`tests/runs/test_react_checkpoint.py:85`、`tests/hooks/test_lifecycle_hooks.py:228` **直接构造 `Run(...)` 并传 `tools=[...]`**。改字段后这三处会 `TypeError`，必须一起改（这是 `Run.open(tools=...)` 便捷路径覆盖不到的地方 —— 它们绕开了 `open`）。改法统一为：

```python
    # 原：tools=[EchoTool()],
    tool_bus=(_bus := bus_with([EchoTool()])),
    activation=ToolActivation(allowed=frozenset(_bus.list_names())),
```

若不想用海象运算符，拆两行：

```python
        bus = bus_with([EchoTool()])
        run = Run(
            ...
            tool_bus=bus,
            activation=ToolActivation(allowed=frozenset(bus.list_names())),
            ...
        )
```

**Interfaces:**
- Consumes: `ToolBus`、`ToolActivation`、`ToolSource`（Task 1、2）
- Produces:
  - `Run.tool_bus: ToolBus`、`Run.activation: ToolActivation`（取代 `Run.tools: list[BaseTool]`）
  - `Run.open(..., tool_bus: ToolBus | None = None, tools: list[BaseTool] | None = None)`
  - `Run.tools` 兼容 property（Task 9 删除）
  - `Run.reload` 透传 bus

- [x] **Step 1: 写失败测试**

在 `tests/runs/test_runner.py` 末尾追加。本文件已有 `StubProvider`（line 34）与 `DelayEchoTool`（line 58，`spec.name == "echo"`），无 agent 构造 helper —— 照 line 113 的样子显式构造 `Agent`：

```python
class RunToolBusTests(unittest.TestCase):
    @staticmethod
    def _agent(tool_ids: list[str]) -> Agent:
        return Agent(
            agent_id="Pickle",
            workspace_path=Path("/tmp/pickle"),
            behavior_path=Path("/tmp/pickle/AGENT.md"),
            behavior_instruction="You are Pickle.",
            model_config=ModelConfig(
                provider="google/gemini",
                model="gemini-3-flash-preview",
            ),
            tool_ids=tool_ids,
            file_access_mode="workspace",
        )

    def test_open_with_tools_builds_a_private_bus_allowing_all_of_them(self) -> None:
        run = Run.open(
            agent=self._agent(tool_ids=[]),
            provider=StubProvider(),
            tools=[DelayEchoTool()],
        )

        self.assertEqual(["echo"], run.tool_bus.list_names())
        self.assertEqual(ToolSource.BUILTIN, run.tool_bus.get("echo").source)
        # tools= 路径忽略 agent.tool_ids，全量允许给定的工具
        self.assertEqual(frozenset({"echo"}), run.activation.allowed)

    def test_open_with_bus_uses_agent_allowlist(self) -> None:
        bus = bus_with([DelayEchoTool(), _OtherTool()])

        run = Run.open(
            agent=self._agent(tool_ids=["echo"]),
            provider=StubProvider(),
            tool_bus=bus,
        )

        self.assertIs(bus, run.tool_bus)
        self.assertEqual(frozenset({"echo"}), run.activation.allowed)
        # bus 里有 other，但白名单只给了 echo
        self.assertEqual(("echo",), run.tool_bus.snapshot(run.activation).names)

    def test_bus_wins_when_both_given(self) -> None:
        bus = bus_with([DelayEchoTool()])

        run = Run.open(
            agent=self._agent(tool_ids=["echo"]),
            provider=StubProvider(),
            tool_bus=bus,
            tools=[_OtherTool()],
        )

        self.assertIs(bus, run.tool_bus)
        self.assertEqual(["echo"], run.tool_bus.list_names())
```

同文件加一个第二工具 stub（`DelayEchoTool` 之后）：

```python
class _OtherTool(BaseTool):
    spec = ToolSpec(
        name="other",
        description="Other tool",
        input_schema={"type": "object", "properties": {}},
    )

    async def execute(
        self,
        arguments: dict[str, object],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        return ToolExecutionResult(content="other")
```

顶部 import 增加：

```python
from pickel.tools.bus import ToolActivation, ToolSource, bus_with
```

并把 `_run` helper（line 88-108）里的 `tools=tools or []` 改为：

```python
) -> Run:
    bus = bus_with(tools or [])
    return Run(
        agent=agent,
        provider=provider,
        tool_bus=bus,
        activation=ToolActivation(allowed=frozenset(bus.list_names())),
        context_assembler=ContextAssembler(),
        ...
    )
```

- [x] **Step 2: 跑测试确认失败**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/runs/test_runner.py -q
```

Expected: FAIL，`AttributeError: 'Run' object has no attribute 'tool_bus'`

- [x] **Step 3: 改 `Run` 的字段**

`src/pickel/runs/run.py`，`Run` dataclass 里把

```python
    tools: list[BaseTool]
```

换成

```python
    tool_bus: ToolBus
    activation: ToolActivation
```

顶部 import：去掉 `from pickel.tools.catalog import builtin_tools` 与 `from pickel.tools.registry import ToolRegistry`，加

```python
from pickel.tools.bus import ToolActivation, ToolBus, bus_with
```

- [x] **Step 4: 改 `Run.open` 的工具解析段**

把

```python
        if tools is None:
            registry = tool_registry or ToolRegistry(tools=builtin_tools())
            resolved_tools = registry.resolve_many(agent.tool_ids)
        else:
            resolved_tools = list(tools)
```

换成

```python
        # tool_bus 优先；只给 tools 时建一个私有 bus 并全量允许（测试便捷路径）
        if tool_bus is not None:
            resolved_bus = tool_bus
            activation = ToolActivation(allowed=frozenset(agent.tool_ids))
        else:
            resolved_bus = bus_with(tools or [])
            activation = ToolActivation(allowed=frozenset(resolved_bus.list_names()))
```

`open()` 签名：删除 `tool_registry: ToolRegistry | None = None`，增加 `tool_bus: ToolBus | None = None`；`tools: list[BaseTool] | None = None` 保留。构造 `cls(...)` 时把 `tools=resolved_tools` 换成 `tool_bus=resolved_bus, activation=activation`。

- [x] **Step 5: 加兼容 property**

`Run` 类内追加（Task 9 删除）：

```python
    @property
    def tools(self) -> list[BaseTool]:
        """兼容旧读法：按当前激活集算一份工具列表。

        Task 6 之后 prepare / react 都走 TurnState 的快照，此 property 仅供尚未迁移的
        调用点与旧测试使用，Task 9 删除。
        """
        return [entry.tool for entry in self.tool_bus.snapshot(self.activation).entries]
```

- [x] **Step 6: 改 `Run.reload` 透传 bus**

`Run.reload` 里 `boot.build_run(...)` 之后、返回之前，加一行把旧 bus 交给新 run —— 但更干净的做法是让 `Boot` 持有 bus（Task 7）。本任务先在 `reload` 里显式保留：

```python
        new_run.tool_bus = old_run.tool_bus
        new_run.activation = ToolActivation(allowed=frozenset(agent.tool_ids))
```

放在 `new_run.environ = old_run.environ` 之后。Task 7 让 `Boot` 注入 bus 后，这两行变成冗余保险，可保留（幂等）。

- [x] **Step 7: 改掉三处直接构造 `Run(...)` 的测试**

```bash
grep -rn "tools=" tests/runs/test_react_checkpoint.py tests/hooks/test_lifecycle_hooks.py tests/runs/test_runner.py | grep -v __pycache__
```

对每处按本任务开头「注意」段的改法替换（`tool_bus=bus` + `activation=ToolActivation(allowed=frozenset(bus.list_names()))`），并在文件顶部 import `from pickel.tools.bus import ToolActivation, bus_with`。

走 `Run.open(...)` 或本地 `_run(...)` helper 的调用点不用动 —— `tools=` 便捷路径与已改的 helper 会接住。

- [x] **Step 8: 跑测试确认通过**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/runs/ tests/config/test_environ.py tests/hooks/ -q 2>&1 | tail -5
```

Expected: PASS。若报 `TypeError: Run.__init__() got an unexpected keyword argument 'tools'`，说明还有直接构造 `Run(...)` 的位置漏改：

```bash
grep -rn "= Run($" tests/ src/ | grep -v __pycache__
```

再确认全量不退化：

```bash
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
```

Expected: `18 failed, <N> passed, 1 skipped` —— **失败数必须仍是 18**，通过数只增不减

- [x] **Step 9: Commit**

```bash
git add src/pickel/runs/run.py tests/runs/test_runner.py tests/runs/test_react_checkpoint.py \
        tests/hooks/test_lifecycle_hooks.py
git commit -m "refactor(runs): Run 持有 ToolBus 与激活集，保留 tools= 便捷路径"
```

---

## Task 5: TurnState 快照 + react 执行改造

**Files:**
- Modify: `src/pickel/runs/turn_state.py`
- Modify: `src/pickel/runs/strategy/react.py`（`execute` 开头、`_execute_tool_call`）
- Test: `tests/runs/test_react_checkpoint.py` 或新建 `tests/runs/test_react_tool_snapshot.py`

**Interfaces:**
- Consumes: `ToolSnapshot`（Task 2）、`Run.tool_bus` / `Run.activation`（Task 4）
- Produces: `TurnState.tool_snapshot: ToolSnapshot | None = None`，在 `ReActStrategy.execute` 的 turn 开始处填充

- [x] **Step 1: 写失败测试**

新建 `tests/runs/test_react_tool_snapshot.py`：

```python
"""turn 边界快照语义：turn 内改 bus 不影响本 turn 的工具集。"""

import unittest

from pickel.runs.turn_state import TurnState
from pickel.tools.base import BaseTool, ToolSpec
from pickel.tools.bus import ToolActivation, ToolBus, ToolSource


def _stub_tool(name: str) -> BaseTool:
    class _Stub(BaseTool):
        spec = ToolSpec(
            name=name,
            description=f"{name} description",
            input_schema={"type": "object", "properties": {}},
        )

    return _Stub()


class TurnStateSnapshotTests(unittest.TestCase):
    def test_turn_state_holds_no_snapshot_by_default(self) -> None:
        self.assertIsNone(TurnState().tool_snapshot)

    def test_snapshot_stays_stable_after_bus_mutation_within_turn(self) -> None:
        bus = ToolBus()
        bus.register(_stub_tool("read_file"), source=ToolSource.BUILTIN)
        activation = ToolActivation(allowed=frozenset({"read_file", "write_file"}))

        turn = TurnState()
        turn.tool_snapshot = bus.snapshot(activation)

        # 模拟 turn 中间发生的热插拔
        bus.register(_stub_tool("write_file"), source=ToolSource.BUILTIN)
        bus.set_enabled("read_file", False)

        self.assertEqual(("read_file",), turn.tool_snapshot.names)
        self.assertIsNotNone(turn.tool_snapshot.find("read_file"))
        self.assertIsNone(turn.tool_snapshot.find("write_file"))


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: 跑测试确认失败**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/runs/test_react_tool_snapshot.py -q
```

Expected: FAIL，`TypeError: TurnState.__init__() got an unexpected keyword argument` 或 `AttributeError: 'TurnState' object has no attribute 'tool_snapshot'`

- [x] **Step 3: 给 `TurnState` 加字段**

`src/pickel/runs/turn_state.py`，`TurnState` 内追加（放 `hook_feedback` 之后）：

```python
    # 本 turn 的工具快照：turn 开始时取一次，turn 内不变
    tool_snapshot: ToolSnapshot | None = None
```

顶部 import 增加 `from pickel.tools.bus import ToolSnapshot`。

- [x] **Step 4: 跑测试确认通过**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/runs/test_react_tool_snapshot.py -q
```

Expected: PASS（全部通过）

- [x] **Step 5: react 在 turn 开始取快照**

`src/pickel/runs/strategy/react.py`，`turn = TurnState()`（约 line 56）之后紧接一行：

```python
        turn.tool_snapshot = run.tool_bus.snapshot(run.activation)
```

- [x] **Step 6: react 执行改用快照**

`_execute_tool_call` 里把

```python
        tool = next(
            (candidate for candidate in run.tools if candidate.spec.name == tool_call.name),
            None,
        )
```

换成

```python
        snapshot = turn.tool_snapshot
        tool = snapshot.find(tool_call.name) if snapshot is not None else None
```

`_execute_tool_call` 与其调用者（`_run_tool_call` 一类的包装方法）签名增加 `turn: TurnState` 关键字参数，从 `execute` 一路传下去。沿调用链把 `turn` 传到位；`turn.tool_snapshot` 为 `None` 时行为与「找不到工具」一致，返回现有的 `Tool 'x' is not available.` 错误结果。

- [x] **Step 7: 跑全量确认不退化**

```bash
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
```

Expected: `18 failed, <N> passed, 1 skipped` —— **失败数必须仍是 18**，通过数只增不减

若 `tests/runs/test_events.py`、`test_react_checkpoint.py` 出现新失败，检查 `turn` 是否漏传到某条调用路径。

- [x] **Step 8: Commit**

```bash
git add src/pickel/runs/turn_state.py src/pickel/runs/strategy/react.py tests/runs/test_react_tool_snapshot.py
git commit -m "feat(runs): turn 边界工具快照，react 执行改用快照查找"
```

---

## Task 6: prepare 改用快照

**Files:**
- Modify: `src/pickel/context/prepare.py`（`resolve_tools`、`prepare`）
- Modify: `src/pickel/runs/strategy/react.py`（调 prepare 处传 snapshot）
- Test: `tests/context/test_prepare.py`

**Interfaces:**
- Consumes: `ToolSnapshot`（Task 2）、`TurnState.tool_snapshot`（Task 5）
- Produces:
  - `resolve_tools(*, snapshot: ToolSnapshot | None) -> list[ToolDefinition]`
  - `prepare(..., snapshot: ToolSnapshot | None = None) -> ModelContext`

- [x] **Step 1: 写失败测试**

`tests/context/test_prepare.py` 是 pytest 函数式，追加：

```python
def test_resolve_tools_uses_entry_name_over_spec_name():
    from pickel.context.prepare import resolve_tools
    from pickel.tools.bus import ToolActivation, ToolBus, ToolSource

    bus = ToolBus()
    bus.register(_EchoTool(), source=ToolSource.MCP, origin="github")
    snapshot = bus.snapshot(ToolActivation(allowed=frozenset({"mcp__github__echo"})))

    definitions = resolve_tools(snapshot=snapshot)

    assert [d.name for d in definitions] == ["mcp__github__echo"]
    assert definitions[0].description == "Echo text"


def test_resolve_tools_returns_empty_for_missing_snapshot():
    from pickel.context.prepare import resolve_tools

    assert resolve_tools(snapshot=None) == []
```

并把该文件已有的 `_run()` helper 与 `test_prepare_system_history_feedback_tools` 改造为传 snapshot：`_run()` 去掉 `tools` 字段，测试里构造

```python
    bus = ToolBus()
    bus.register(_EchoTool(), source=ToolSource.BUILTIN)
    snapshot = bus.snapshot(ToolActivation(allowed=frozenset({"echo"})))
```

并把 `prepare(...)` 调用加上 `snapshot=snapshot`；原断言 `[t.name for t in ctx.tools] == ["echo"]` 保持不变。

- [x] **Step 2: 跑测试确认失败**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/context/test_prepare.py -q
```

Expected: FAIL，`TypeError: resolve_tools() got an unexpected keyword argument 'snapshot'`

- [x] **Step 3: 改 `resolve_tools`**

`src/pickel/context/prepare.py`：

```python
def resolve_tools(*, snapshot: ToolSnapshot | None) -> list[ToolDefinition]:
    """工具快照 → ToolDefinition 列表。

    name 取 ToolEntry.name（含命名空间前缀），不是 tool.spec.name ——
    模型看到的名字必须与快照查找键一致。
    """
    if snapshot is None:
        return []
    return [
        ToolDefinition(
            name=entry.name,
            description=entry.tool.spec.description,
            input_schema=entry.tool.spec.input_schema,
        )
        for entry in snapshot.entries
    ]
```

顶部 import 增加 `from pickel.tools.bus import ToolSnapshot`。

- [x] **Step 4: 改 `prepare` 签名与调用**

`prepare()` 增加关键字参数 `snapshot: ToolSnapshot | None = None`，函数体内 `tools = resolve_tools(run=run)` 改为 `tools = resolve_tools(snapshot=snapshot)`。

- [x] **Step 5: react 传 snapshot**

`src/pickel/runs/strategy/react.py` 里 `await prepare(...)`（约 line 72）的调用增加：

```python
                snapshot=turn.tool_snapshot,
```

- [x] **Step 6: 检查其他 prepare / resolve_tools 调用点**

```bash
grep -rn "resolve_tools\|prepare(" src/ tests/ | grep -v __pycache__ | grep -v "def prepare"
```

对每个命中点确认是否需要传 `snapshot`。`/context` 预览路径（`cli/` 下）若调 prepare，需要同样从 bus 取一份快照传入 —— 用 `run.tool_bus.snapshot(run.activation)`。`tests/context/test_skills_hot_reload.py` 若用 `SimpleNamespace(..., tools=[])` 假 run 且只测 system 段，无需改动。

- [x] **Step 7: 跑全量确认不退化**

```bash
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
```

Expected: `18 failed, <N> passed, 1 skipped` —— **失败数必须仍是 18**，通过数只增不减

- [x] **Step 8: Commit**

```bash
git add src/pickel/context/prepare.py src/pickel/runs/strategy/react.py tests/context/test_prepare.py
git commit -m "refactor(context): prepare 改用 turn 工具快照"
```

---

## Task 7: catalog 装配 + Boot / ChatApp 注入

让 bus 成为真正的进程级对象，跨 `/reload` 存活。

**Files:**
- Modify: `src/pickel/tools/catalog.py`
- Modify: `src/pickel/app/boot.py`
- Modify: `src/pickel/cli/chat.py`
- Test: `tests/app/test_boot_tool_bus.py`（新建；若 `tests/app/` 不存在则建目录与 `__init__.py`）

**Interfaces:**
- Consumes: `ToolBus`、`ToolSource`（Task 1）
- Produces:
  - `install_builtin_tools(bus: ToolBus) -> None`
  - `Boot.__init__(app_config, tool_bus: ToolBus | None = None)`、`Boot.from_config(app_config, tool_bus=None)`
  - `Boot.tool_bus` 属性；`Boot.build_run` 透传

- [x] **Step 1: 写失败测试**

新建 `tests/app/test_boot_tool_bus.py`：

```python
"""Boot 透传进程级 bus；reload 后仍是同一实例。"""

import unittest

from pickel.tools.bus import ToolBus, ToolSource
from pickel.tools.catalog import install_builtin_tools


class InstallBuiltinToolsTests(unittest.TestCase):
    def test_install_registers_every_builtin_as_builtin_source(self) -> None:
        bus = ToolBus()

        install_builtin_tools(bus)

        names = bus.list_names(source=ToolSource.BUILTIN)
        self.assertIn("read_file", names)
        self.assertIn("shell_exec", names)
        self.assertIn("echo", names)
        self.assertEqual(sorted(names), sorted(bus.list_names()))

    def test_install_is_idempotent(self) -> None:
        bus = ToolBus()

        install_builtin_tools(bus)
        first = sorted(bus.list_names())
        install_builtin_tools(bus)

        self.assertEqual(first, sorted(bus.list_names()))


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: 跑测试确认失败**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/app/test_boot_tool_bus.py -q
```

Expected: FAIL，`ImportError: cannot import name 'install_builtin_tools'`

- [x] **Step 3: 实现 `install_builtin_tools`**

`src/pickel/tools/catalog.py` 末尾追加：

```python
def install_builtin_tools(bus: ToolBus) -> None:
    """把内置工具装进总线。重复调用幂等（同来源同 origin 覆盖）。"""
    for tool in builtin_tools():
        bus.register(tool, source=ToolSource.BUILTIN)
```

顶部 import 增加 `from pickel.tools.bus import ToolBus, ToolSource`。

- [x] **Step 4: Boot 接受并透传 bus**

`src/pickel/app/boot.py`：

```python
    def __init__(self, app_config: AppConfig, tool_bus: ToolBus | None = None) -> None:
        self.app_config = app_config
        if tool_bus is None:
            tool_bus = ToolBus()
            install_builtin_tools(tool_bus)
        self.tool_bus = tool_bus

    @classmethod
    def from_config(
        cls,
        app_config: AppConfig,
        tool_bus: ToolBus | None = None,
    ) -> "Boot":
        return cls(app_config, tool_bus=tool_bus)
```

`build_run` 里 `Run.open(...)` 增加 `tool_bus=self.tool_bus`。顶部 import 增加：

```python
from pickel.tools.bus import ToolBus
from pickel.tools.catalog import install_builtin_tools
```

- [x] **Step 5: ChatApp 持有 bus 并在 /reload 复用**

`src/pickel/cli/chat.py`：

- `__init__` 里存 `self._tool_bus = boot.tool_bus if boot is not None else None`
- `_handle_reload_command` 里 `boot = Boot.from_config(app_config)` 改为

```python
            boot = Boot.from_config(app_config, tool_bus=self._tool_bus)
```

- 若 `self._tool_bus` 为 `None`（未传 boot 的路径），reload 后回填 `self._tool_bus = boot.tool_bus`
- `/reload` 帮助文本（约 line 232）改为 `"/reload            Reload disk config/skills/agent (keep Environ and tool bus)\n"`

- [x] **Step 6: 追加 reload 复用测试**

在 `tests/app/test_boot_tool_bus.py` 追加：

```python
class BootToolBusTests(unittest.TestCase):
    def test_boot_creates_bus_with_builtins_when_not_injected(self) -> None:
        from pickel.app.boot import Boot

        boot = Boot.from_config(SimpleNamespace())

        self.assertIn("read_file", boot.tool_bus.list_names())

    def test_boot_reuses_injected_bus(self) -> None:
        from pickel.app.boot import Boot

        bus = ToolBus()
        install_builtin_tools(bus)

        boot = Boot.from_config(SimpleNamespace(), tool_bus=bus)

        self.assertIs(bus, boot.tool_bus)
```

用 `SimpleNamespace()` 而非真 `AppConfig`：`AppConfig` 有 4 个必填字段（`default_agent` / `default_llm` / `providers` / `agents`），而 `Boot.__init__` 只把 `app_config` 存下来、不校验 —— 这两个用例只关心 bus 的装配与复用。文件顶部加 `from types import SimpleNamespace`。

- [x] **Step 7: 跑全量确认不退化**

```bash
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
```

Expected: `18 failed, <N> passed, 1 skipped` —— **失败数必须仍是 18**，通过数只增不减

- [x] **Step 8: 手动验收 —— bus 真的跨 reload 存活**

```bash
uv run pickel chat
```

在会话里输入 `/reload`，确认无异常、工具仍可用（例如让它 `read_file` 读 `README.md` 的前几行）。退出。

- [x] **Step 9: Commit**

```bash
git add src/pickel/tools/catalog.py src/pickel/app/boot.py src/pickel/cli/chat.py tests/app/
git commit -m "feat(app): 进程级 ToolBus 由 ChatApp 持有，跨 /reload 存活"
```

---

## Task 8: `tool_set_active` 内置工具

agent 自我收窄激活集的入口，下一 turn 生效。

**Files:**
- Modify: `src/pickel/tools/builtin.py`
- Modify: `src/pickel/tools/catalog.py`（加进 `builtin_tools()`）
- Test: `tests/tools/test_builtin.py`（追加）

**Interfaces:**
- Consumes: `ToolActivation.with_agent_disabled` / `with_agent_enabled`（Task 2）、`Run.activation`（Task 4）
- Produces: 名为 `tool_set_active` 的内置工具，入参 `{disable?: string[], enable?: string[]}`

工具需要写 `run.activation`，而 `ToolExecutionContext` 不含 `run`。**不要为此往 context 加 `run` 字段**（会把工具层耦到运行层）。改为在 `ToolServices` 加一个窄接口：

```python
@dataclass(frozen=True)
class ToolServices:
    workspace_files: "WorkspaceFileService | None" = None
    shell_sessions: "ShellSessionManager | None" = None
    activation_control: "ActivationControl | None" = None
```

`ActivationControl` 定义在 `src/pickel/tools/services.py`：

```python
class ActivationControl(Protocol):
    """让工具收窄/恢复激活集的窄接口。实现方是 Run。"""

    def allowed_names(self) -> frozenset[str]: ...
    def disable_tools(self, names: Iterable[str]) -> None: ...
    def enable_tools(self, names: Iterable[str]) -> None: ...
```

`Run` 实现这三个方法（改 `self.activation`），`get_tool_execution_context` 里传 `activation_control=self`。

- [x] **Step 1: 写失败测试**

`tests/tools/test_builtin.py` 追加：

```python
class ToolSetActiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_disable_narrows_activation_for_next_turn(self) -> None:
        from pickel.tools.builtin import tool_set_active
        from pickel.tools.services import ToolServices

        control = _FakeActivationControl(allowed={"read_file", "shell_exec"})
        context = ToolExecutionContext(
            agent_id="Pickle",
            session_id="session-1",
            workspace_path=Path("/tmp/pickle"),
            services=ToolServices(activation_control=control),
        )

        result = await tool_set_active.execute({"disable": ["shell_exec"]}, context)

        self.assertFalse(result.is_error)
        self.assertEqual({"shell_exec"}, control.disabled)
        self.assertIn("next turn", result.content.lower())

    async def test_enable_restores_previously_disabled_tool(self) -> None:
        from pickel.tools.builtin import tool_set_active
        from pickel.tools.services import ToolServices

        control = _FakeActivationControl(allowed={"read_file"}, disabled={"read_file"})
        context = ToolExecutionContext(
            agent_id="Pickle",
            session_id="session-1",
            workspace_path=Path("/tmp/pickle"),
            services=ToolServices(activation_control=control),
        )

        result = await tool_set_active.execute({"enable": ["read_file"]}, context)

        self.assertFalse(result.is_error)
        self.assertEqual(set(), control.disabled)

    async def test_enabling_tool_outside_allowlist_is_an_error(self) -> None:
        from pickel.tools.builtin import tool_set_active
        from pickel.tools.services import ToolServices

        control = _FakeActivationControl(allowed={"read_file"})
        context = ToolExecutionContext(
            agent_id="Pickle",
            session_id="session-1",
            workspace_path=Path("/tmp/pickle"),
            services=ToolServices(activation_control=control),
        )

        result = await tool_set_active.execute({"enable": ["shell_exec"]}, context)

        self.assertTrue(result.is_error)
        self.assertIn("shell_exec", result.content)
        self.assertEqual(set(), control.disabled)

    async def test_missing_activation_control_is_an_error(self) -> None:
        from pickel.tools.builtin import tool_set_active

        context = ToolExecutionContext(
            agent_id="Pickle",
            session_id="session-1",
            workspace_path=Path("/tmp/pickle"),
        )

        result = await tool_set_active.execute({"disable": ["read_file"]}, context)

        self.assertTrue(result.is_error)
```

文件内加 fake：

```python
class _FakeActivationControl:
    def __init__(self, *, allowed: set[str], disabled: set[str] | None = None) -> None:
        self._allowed = frozenset(allowed)
        self.disabled = set(disabled or set())

    def allowed_names(self) -> frozenset[str]:
        return self._allowed

    def disable_tools(self, names) -> None:
        self.disabled |= set(names)

    def enable_tools(self, names) -> None:
        self.disabled -= set(names)
```

- [x] **Step 2: 跑测试确认失败**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/tools/test_builtin.py -q
```

Expected: FAIL，`ImportError: cannot import name 'tool_set_active'`

- [x] **Step 3: 加 `ActivationControl` 与 `ToolServices` 字段**

按本任务开头的代码改 `src/pickel/tools/services.py`（`Protocol` 从 `typing` 导入，`Iterable` 从 `collections.abc` 导入；`ActivationControl` 是运行期需要的真实名字，不放 `TYPE_CHECKING` 块内）。

- [x] **Step 4: `Run` 实现三个方法**

`src/pickel/runs/run.py`，`Run` 类内追加：

```python
    def allowed_names(self) -> frozenset[str]:
        return self.activation.allowed

    def disable_tools(self, names: Iterable[str]) -> None:
        """agent 自我收窄激活集，下一 turn 生效（本 turn 快照已取）。"""
        self.activation = self.activation.with_agent_disabled(names)

    def enable_tools(self, names: Iterable[str]) -> None:
        self.activation = self.activation.with_agent_enabled(names)
```

`get_tool_execution_context` 的 `ToolServices(...)` 增加 `activation_control=self`。顶部 import 增加 `from collections.abc import Iterable`。

- [x] **Step 5: 实现 `tool_set_active`**

`src/pickel/tools/builtin.py` 追加：

```python
@tool(
    name="tool_set_active",
    description=(
        "Narrow or restore which tools are exposed to you. "
        "Changes take effect on the NEXT turn, not the current one. "
        "You can only disable or re-enable tools already granted to this agent; "
        "you cannot add new tools."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "disable": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Tool names to hide from yourself starting next turn.",
            },
            "enable": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Previously disabled tool names to restore.",
            },
        },
    },
)
async def tool_set_active(
    arguments: dict,
    context: ToolExecutionContext,
) -> ToolExecutionResult:
    control = context.services.activation_control
    if control is None:
        return ToolExecutionResult(
            content="Activation control is not available in this context.",
            is_error=True,
        )

    to_disable = [str(name) for name in (arguments.get("disable") or [])]
    to_enable = [str(name) for name in (arguments.get("enable") or [])]
    if not to_disable and not to_enable:
        return ToolExecutionResult(
            content="Nothing to do: provide 'disable' and/or 'enable'.",
            is_error=True,
        )

    allowed = control.allowed_names()
    unauthorized = sorted({name for name in to_enable if name not in allowed})
    if unauthorized:
        return ToolExecutionResult(
            content=(
                f"Not granted to this agent: {', '.join(unauthorized)}. "
                "tool_set_active can only restore tools already in the agent's allowlist."
            ),
            is_error=True,
        )

    if to_disable:
        control.disable_tools(to_disable)
    if to_enable:
        control.enable_tools(to_enable)

    changes = []
    if to_disable:
        changes.append(f"disabled {', '.join(sorted(set(to_disable)))}")
    if to_enable:
        changes.append(f"enabled {', '.join(sorted(set(to_enable)))}")
    return ToolExecutionResult(
        content=f"Tool activation updated ({'; '.join(changes)}). Takes effect next turn.",
        metadata={"disabled": sorted(set(to_disable)), "enabled": sorted(set(to_enable))},
    )
```

顶部 import 增加 `ToolExecutionResult`。

- [x] **Step 6: 加进 `builtin_tools()`**

`src/pickel/tools/catalog.py` 的 `builtin_tools()` 返回列表里，`echo` 之后加 `tool_set_active`；顶部 import 相应增加。

- [x] **Step 7: 跑测试确认通过**

```bash
uv run --with pytest --with pytest-asyncio pytest tests/tools/test_builtin.py tests/app/ -q
```

Expected: PASS

```bash
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
```

Expected: `18 failed, <N> passed, 1 skipped` —— **失败数必须仍是 18**，通过数只增不减

- [x] **Step 8: Commit**

```bash
git add src/pickel/tools/builtin.py src/pickel/tools/catalog.py src/pickel/tools/services.py \
        src/pickel/runs/run.py tests/tools/test_builtin.py
git commit -m "feat(tools): tool_set_active 让 agent 自我收窄激活集"
```

---

## Task 9: 删除 ToolRegistry 与兼容层

**Files:**
- Delete: `src/pickel/tools/registry.py`、`tests/tools/test_registry.py`
- Modify: `src/pickel/tools/__init__.py`
- Modify: `src/pickel/runs/run.py`（删 `tools` 兼容 property）
- Modify: `docs/upgrade/2026-07-26-tool-bus-design.md`（校对与实现一致）

- [x] **Step 1: 确认没有残留引用**

```bash
grep -rn "ToolRegistry\|tools.registry\|tool_registry" src/ tests/ | grep -v __pycache__
grep -rn "run\.tools\|self\.tools\|\.tools\b" src/pickel/runs/ src/pickel/context/ | grep -v __pycache__
```

Expected: 第一条只剩 `src/pickel/tools/registry.py` 与 `tests/tools/test_registry.py` 自身、以及 `tools/__init__.py` 的导出；第二条不再有读 `run.tools` 的地方（`model_context.tools` / `request.tools` 是另一回事，保留）。

- [x] **Step 2: 删除文件与导出**

```bash
git rm src/pickel/tools/registry.py tests/tools/test_registry.py
```

`src/pickel/tools/__init__.py` 去掉 `ToolRegistry` 的 import 与 `__all__` 条目，改为导出总线相关名字：

```python
from pickel.tools.base import (
    BaseTool,
    FunctionTool,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolSpec,
    tool,
)
from pickel.tools.bus import (
    ToolActivation,
    ToolBus,
    ToolEntry,
    ToolNameConflictError,
    ToolSnapshot,
    ToolSource,
)
from pickel.tools.services import ActivationControl, ToolServices

__all__ = [
    "ActivationControl",
    "BaseTool",
    "FunctionTool",
    "ToolActivation",
    "ToolBus",
    "ToolEntry",
    "ToolExecutionContext",
    "ToolExecutionResult",
    "ToolNameConflictError",
    "ToolServices",
    "ToolSnapshot",
    "ToolSource",
    "ToolSpec",
    "tool",
]
```

- [x] **Step 3: 删 `Run.tools` 兼容 property**

`src/pickel/runs/run.py` 删掉 Task 4 Step 5 加的 `tools` property。`Run.open` 的 `tools=` 参数**保留**（测试便捷路径，设计稿 2.4 已定）。

- [x] **Step 4: 跑全量确认不退化**

```bash
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
```

Expected: `18 failed, <N> passed, 1 skipped` —— **失败数必须仍是 18**，通过数只增不减（少掉 test_registry.py 的 2 个用例）

失败清单须与基线逐条一致：

```bash
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | grep FAILED | sort > /tmp/t1-after.txt
```

确认其中只有 `tests/tools/test_shell.py`（6 例）与 `tests/providers/`（12 例）。

- [x] **Step 5: 手动验收**

```bash
uv run pickel chat
```

- 让它读一个文件（验证 file 工具经 `ToolServices` 走通）
- 让它跑一条 shell 命令（验证 shell 工具经 `ToolServices` 走通）
- 让它调用 `tool_set_active` 关掉 `shell_exec`，然后在**下一轮**要求它跑 shell 命令 —— 应报「工具不可用」
- `/reload` 后确认工具仍在

- [x] **Step 6: 校对设计稿**

按实际实现修订 `docs/upgrade/2026-07-26-tool-bus-design.md`：
- 3.4 节 `ToolServices` 补上 `activation_control` 字段与 `ActivationControl` 协议（Task 8 引入，设计稿写时未有）
- 3.5 节补上 `tool_set_active` 通过 `ActivationControl` 而非 `run` 直连的说明
- 5 节改动清单补 `tools/services.py` 的 `ActivationControl`

- [x] **Step 7: Commit**

```bash
git add -A src/pickel/tools/ src/pickel/runs/run.py docs/upgrade/2026-07-26-tool-bus-design.md
git commit -m "refactor(tools): 删除 ToolRegistry 与 Run.tools 兼容层，校对设计稿"
```

---

## 完成标准

1. `uv run --with pytest --with pytest-asyncio pytest -q` 的失败集合与基线逐条相同（只有 6 例 shell ANSI + 12 例 provider 缺 key）。
2. `grep -rn "ToolRegistry" src/ tests/` 无命中。
3. `grep -rn "workspace_files=\|shell_session_manager=" src/pickel/tools/base.py` 无命中。
4. `pickel chat` 里文件工具、shell 工具、`tool_set_active`、`/reload` 四项手动验收通过。
5. 设计稿与实现一致（Task 9 Step 6 已校对）。

## 已知不在本计划内

| 项 | 归属 |
| --- | --- |
| `tests/tools/test_shell.py` 的 6 例 ANSI 失败 | S1 |
| `tests/providers/` 的 12 例缺 key 失败 | 环境，非代码问题 |
| extension 宿主与装载器（hook handler / recall source / 命令 / skill 路径的注册） | E1 |
| MCP 客户端 | T2（实现为一个内置 extension） |
| 沙箱、跨 agent 进程隔离 | S2 |
| `ToolEntry.version` 的填充逻辑、skill 版本与审批 | V1 |
