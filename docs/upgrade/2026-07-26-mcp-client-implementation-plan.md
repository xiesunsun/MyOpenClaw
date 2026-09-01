# T2 MCP 客户端实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 stdio MCP server 的工具接入 pickel 工具总线，实现为内置 extension `mcp`。

**Architecture:** 每个 server 一个 `McpConnection`（专属背景任务持有 SDK 的 async-with 栈）+ 一个 `McpServerRuntime`（注册/重连/调用编排）；`McpProxyTool` 把 MCP 工具转成 `BaseTool`。配置读 `.mcp.json`（全局+项目合并）。宿主增 `register_mcp_tool`/`unregister_mcp_origin`/`app_config` 三个扩展点；总线白名单增通配。

**Tech Stack:** Python 3.12、mcp>=1.26.0（已声明）、pytest + pytest-asyncio、FastMCP 测试夹具。

## Global Constraints

- 设计稿：`docs/upgrade/2026-07-26-mcp-client-design.md`。范围外：HTTP/SSE、resources/prompts/sampling、审批流、后台健康检查。
- 命名 `mcp__<server>__<tool>`（bus 的 `qualified_name` 已实现，`source=ToolSource.MCP, origin=<server>`）。
- 单 server 任何失败都隔离：记 warning 跳过，不阻断启动、不影响其他 server。
- 连接建立（spawn+initialize+list_tools）超时 10s；单次工具调用超时 60s。
- 测试命令：`uv run --with pytest --with pytest-asyncio pytest <path> -q`。全量基线：12 failed（全缺 key），分布不得变化。
- 凭证不写进代码或提交。

**测试文件公共 helper**（Task 3 首次建 `tests/extensions/mcp/__init__.py` 与各测试文件；`_context` 参照 `tests/tools/test_shell.py` 末尾的同名 helper）。

---

### Task 1: ToolBus 白名单通配

**Files:**
- Modify: `src/pickel/tools/bus.py`（`snapshot` / `missing_names`）
- Test: `tests/tools/test_bus.py`

**Interfaces:**
- Produces: `ToolActivation.allowed` 中含 `*` 的元素按 `fnmatch.fnmatchcase` 作为模式匹配；不含 `*` 的元素语义不变。

- [ ] **Step 1: 写失败测试**（追加到 `tests/tools/test_bus.py`，import 按文件现状补）

```python
class WildcardActivationTests(unittest.TestCase):
    def _bus_with_mcp_tools(self) -> ToolBus:
        bus = ToolBus()
        for tool_name in ("create_issue", "list_repos"):
            bus.register(
                FunctionTool(
                    name=tool_name, description="d", input_schema={"type": "object"},
                    func=lambda a, c: ToolExecutionResult(content="ok"),
                ),
                source=ToolSource.MCP,
                origin="github",
            )
        return bus

    def test_snapshot_matches_wildcard_pattern(self) -> None:
        bus = self._bus_with_mcp_tools()
        snapshot = bus.snapshot(ToolActivation(allowed=frozenset({"mcp__github__*"})))
        self.assertEqual(
            {"mcp__github__create_issue", "mcp__github__list_repos"},
            set(snapshot.names),
        )

    def test_snapshot_exact_names_still_work(self) -> None:
        bus = self._bus_with_mcp_tools()
        snapshot = bus.snapshot(
            ToolActivation(allowed=frozenset({"mcp__github__create_issue"}))
        )
        self.assertEqual(("mcp__github__create_issue",), snapshot.names)

    def test_agent_disabled_is_exact_and_beats_wildcard(self) -> None:
        bus = self._bus_with_mcp_tools()
        snapshot = bus.snapshot(
            ToolActivation(
                allowed=frozenset({"mcp__github__*"}),
                agent_disabled=frozenset({"mcp__github__create_issue"}),
            )
        )
        self.assertEqual(("mcp__github__list_repos",), snapshot.names)

    def test_missing_names_wildcard_only_when_nothing_matches(self) -> None:
        bus = self._bus_with_mcp_tools()
        activation = ToolActivation(
            allowed=frozenset({"mcp__github__*", "mcp__jira__*", "read_file"})
        )
        self.assertEqual(["mcp__jira__*", "read_file"], bus.missing_names(activation))
```

- [ ] **Step 2: 确认失败** `uv run --with pytest --with pytest-asyncio pytest tests/tools/test_bus.py -q` → 通配相关 4 例 FAIL
- [ ] **Step 3: 实现**（`src/pickel/tools/bus.py`；文件顶部补 `import fnmatch`）

模块级 helper：

```python
def _activation_allows(name: str, allowed: frozenset[str]) -> bool:
    if name in allowed:
        return True
    return any(
        "*" in pattern and fnmatch.fnmatchcase(name, pattern) for pattern in allowed
    )
```

`snapshot` 的过滤条件 `entry.name in activation.allowed` 改为 `_activation_allows(entry.name, activation.allowed)`（`agent_disabled` 判断保持精确名不动）。

`missing_names` 改为：

```python
    def missing_names(self, activation: ToolActivation) -> list[str]:
        """白名单里存在、bus 中却没有的名字。调用方据此记 warning，不视为错误。

        通配模式（含 *）只有在 bus 里没有任何名字匹配它时才算 missing。
        """
        missing = []
        for name in sorted(activation.allowed):
            if "*" in name:
                if not any(
                    fnmatch.fnmatchcase(existing, name) for existing in self._entries
                ):
                    missing.append(name)
            elif name not in self._entries:
                missing.append(name)
        return missing
```

- [ ] **Step 4: 通过** `uv run --with pytest --with pytest-asyncio pytest tests/tools/ -q` 全绿
- [ ] **Step 5: Commit** `git add src/pickel/tools/bus.py tests/tools/test_bus.py && git commit -m "feat(tools): 白名单支持 mcp__server__* 通配"`

---

### Task 2: ExtensionHost 的 MCP 注册接口与 app_config

**Files:**
- Modify: `src/pickel/extensions_host/host.py`
- Modify: `src/pickel/extensions_host/loader.py`（构造 host 时传 app_config）
- Test: `tests/extensions_host/test_host.py`

**Interfaces:**
- Consumes: `ToolBus.register(tool, source, origin)`、`ToolBus.unregister_origin(source, origin)`（已有）。
- Produces:
  - `ExtensionHost.register_mcp_tool(tool: BaseTool, *, server: str) -> str`（返回 `mcp__<server>__<tool>`）
  - `ExtensionHost.unregister_mcp_origin(server: str) -> list[str]`
  - `ExtensionHost.app_config`（loader 传入的 app_config 对象，测试可传假对象）

- [ ] **Step 1: 写失败测试**（追加到 `tests/extensions_host/test_host.py`，构造方式参照该文件既有用例）

```python
class McpHostApiTests(unittest.TestCase):
    def _host(self, bus: ToolBus) -> ExtensionHost:
        return ExtensionHost(
            name="mcp",
            config_section=None,
            tool_bus=bus,
            registry=ExtensionRegistry(),
            app_config=SimpleNamespace(root=Path("/tmp/project")),
        )

    def _tool(self, name: str) -> FunctionTool:
        return FunctionTool(
            name=name, description="d", input_schema={"type": "object"},
            func=lambda a, c: ToolExecutionResult(content="ok"),
        )

    def test_register_mcp_tool_uses_mcp_prefix_and_server_origin(self) -> None:
        bus = ToolBus()
        host = self._host(bus)
        qualified = host.register_mcp_tool(self._tool("create_issue"), server="github")
        self.assertEqual("mcp__github__create_issue", qualified)
        self.assertEqual("github", bus.get(qualified).origin)

    def test_unregister_mcp_origin_removes_only_that_server(self) -> None:
        bus = ToolBus()
        host = self._host(bus)
        host.register_mcp_tool(self._tool("a"), server="github")
        host.register_mcp_tool(self._tool("b"), server="jira")
        removed = host.unregister_mcp_origin("github")
        self.assertEqual(["mcp__github__a"], removed)
        self.assertEqual(["mcp__jira__b"], bus.list_names(source=ToolSource.MCP))

    def test_app_config_is_exposed(self) -> None:
        host = self._host(ToolBus())
        self.assertEqual(Path("/tmp/project"), host.app_config.root)
```

（import 补 `from types import SimpleNamespace`、`from pathlib import Path`。）

- [ ] **Step 2: 确认失败**（`app_config` 参数不存在 → TypeError）
- [ ] **Step 3: 实现**

`host.py`：`__init__` 增关键字参数 `app_config: Any = None`，存 `self.app_config = app_config`；新增两个方法：

```python
    def register_mcp_tool(self, tool: BaseTool, *, server: str) -> str:
        """注册 MCP 代理工具。最终名为 mcp__<server>__<tool>。

        与 register_tool 的 ext__ 前缀分开：MCP 工具跑在子进程里，
        执行位置与信任级别不同，名字上必须能区分（T1 设计）。
        """
        return self._tool_bus.register(tool, source=ToolSource.MCP, origin=server)

    def unregister_mcp_origin(self, server: str) -> list[str]:
        """卸掉某个 MCP server 的全部工具（断连/重连失败路径）。"""
        return self._tool_bus.unregister_origin(ToolSource.MCP, server)
```

`loader.py`：`load_extensions_async` 里构造 `ExtensionHost(...)` 处增 `app_config=app_config`。

- [ ] **Step 4: 通过** `uv run --with pytest --with pytest-asyncio pytest tests/extensions_host/ -q` 全绿
- [ ] **Step 5: Commit** `git add src/pickel/extensions_host tests/extensions_host && git commit -m "feat(extensions): 宿主增 MCP 注册接口与 app_config"`

---

### Task 3: `.mcp.json` 配置模块

**Files:**
- Create: `src/pickel/extensions/mcp/__init__.py`（本 task 先空文件占位，Task 6 填 setup）
- Create: `src/pickel/extensions/mcp/config.py`
- Create: `tests/extensions/mcp/__init__.py`（空）
- Test: `tests/extensions/mcp/test_config.py`

**Interfaces:**
- Produces:
  - `McpServerSpec`：frozen dataclass，字段 `name: str`、`command: str`、`args: tuple[str, ...]`、`env: dict[str, str]`
  - `load_mcp_servers(*, home: Path, project_root: Path) -> dict[str, McpServerSpec]`（全局 `home/.mcp.json` + 项目 `project_root/.mcp.json` 合并，项目覆盖同名；任何失败不抛只 warning）

**注意**：`src/pickel/extensions/mcp/__init__.py` 一旦存在且无 `setup`，内置发现会把它记为装载错误（`has no setup(host) function`）。本 task 的占位文件写一个空 setup 防止污染既有 loader 测试：

```python
async def setup(host) -> None:  # Task 6 替换为真实实现
    return
```

- [ ] **Step 1: 写失败测试**（`tests/extensions/mcp/test_config.py`）

```python
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from pickel.extensions.mcp.config import McpServerSpec, load_mcp_servers


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
            home.mkdir(); proj.mkdir()
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
            home.mkdir(); proj.mkdir()
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
```

- [ ] **Step 2: 确认失败**（模块不存在 → ImportError）
- [ ] **Step 3: 实现**（`src/pickel/extensions/mcp/config.py`）

```python
"""`.mcp.json` 的发现、解析与合并。

格式与 Claude Code 兼容：{"mcpServers": {"<name>": {"command", "args", "env"}}}。
任何一层失败都不抛：坏文件整体跳过、坏 server 单独跳过，只记 warning——
配置问题不该阻断启动（与 extension 装载失败隔离同语义）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import re

logger = logging.getLogger(__name__)

_ENV_PATTERN = re.compile(r"\$\{(\w+)\}")


@dataclass(frozen=True)
class McpServerSpec:
    name: str
    command: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


def load_mcp_servers(*, home: Path, project_root: Path) -> dict[str, McpServerSpec]:
    merged: dict[str, McpServerSpec] = {}
    for path in (home / ".mcp.json", project_root / ".mcp.json"):
        merged.update(_load_file(path))
    return merged


def _load_file(path: Path) -> dict[str, McpServerSpec]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        raw_servers = data["mcpServers"]
    except Exception:
        logger.warning("Ignoring invalid .mcp.json at %s", path, exc_info=True)
        return {}
    specs: dict[str, McpServerSpec] = {}
    for name, raw in raw_servers.items():
        if "__" in name:
            logger.warning(
                "Skipping MCP server '%s': name must not contain '__'", name
            )
            continue
        try:
            specs[name] = McpServerSpec(
                name=name,
                command=str(raw["command"]),
                args=tuple(str(item) for item in raw.get("args", [])),
                env={
                    key: _expand(str(value))
                    for key, value in (raw.get("env") or {}).items()
                },
            )
        except Exception:
            logger.warning(
                "Skipping invalid MCP server '%s' in %s", name, path, exc_info=True
            )
    return specs


def _expand(value: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        env_value = os.environ.get(match.group(1))
        if env_value is None:
            logger.warning(
                "MCP env: %s is not set; keeping literal", match.group(0)
            )
            return match.group(0)
        return env_value

    return _ENV_PATTERN.sub(_replace, value)
```

- [ ] **Step 4: 通过** `uv run --with pytest --with pytest-asyncio pytest tests/extensions/mcp/ -q`；另跑 `tests/extensions_host/ -q` 确认占位 setup 未破坏 loader 测试
- [ ] **Step 5: Commit** `git add src/pickel/extensions/mcp tests/extensions/mcp && git commit -m "feat(mcp): .mcp.json 配置发现、合并与 env 展开"`

---

### Task 4: 测试夹具 server 与 McpConnection

**Files:**
- Create: `src/pickel/extensions/mcp/connection.py`
- Create: `tests/extensions/mcp/fixture_server.py`
- Test: `tests/extensions/mcp/test_connection.py`

**Interfaces:**
- Consumes: `McpServerSpec`（Task 3）。
- Produces:
  - `McpConnectionError(RuntimeError)`
  - `McpConnection(spec)`：`await open()`（spawn+initialize+list_tools，失败/10s 超时抛 `McpConnectionError`）；`tools: list[mcp.types.Tool]`；`is_alive() -> bool`；`await call_tool(name, arguments) -> mcp.types.CallToolResult`（60s 超时；连接死抛 `McpConnectionError`）；`await close()`

- [ ] **Step 1: 写夹具 server**（`tests/extensions/mcp/fixture_server.py`）

```python
"""测试用最小 stdio MCP server。测试以 sys.executable spawn 本文件。"""

import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fixture")


@mcp.tool()
def echo(text: str) -> str:
    """Echo the input text back."""
    return f"echo:{text}"


@mcp.tool()
def boom() -> str:
    """Always fails with an error."""
    raise RuntimeError("boom")


@mcp.tool()
def die() -> str:
    """Exit the server process immediately (for reconnect tests)."""
    os._exit(1)


if __name__ == "__main__":
    mcp.run()
```

- [ ] **Step 2: 写失败测试**（`tests/extensions/mcp/test_connection.py`）

```python
from pathlib import Path
import sys
import unittest

from pickel.extensions.mcp.config import McpServerSpec
from pickel.extensions.mcp.connection import McpConnection, McpConnectionError

FIXTURE = Path(__file__).parent / "fixture_server.py"


def fixture_spec(name: str = "fixture") -> McpServerSpec:
    return McpServerSpec(name=name, command=sys.executable, args=(str(FIXTURE),))


class McpConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_discovers_tools_and_call_roundtrips(self) -> None:
        connection = McpConnection(fixture_spec())
        try:
            await connection.open()
            self.assertTrue(connection.is_alive())
            self.assertEqual(
                {"boom", "die", "echo"},
                {tool.name for tool in connection.tools},
            )
            result = await connection.call_tool("echo", {"text": "hi"})
            self.assertFalse(bool(result.isError))
            self.assertEqual("echo:hi", result.content[0].text)
        finally:
            await connection.close()
        self.assertFalse(connection.is_alive())

    async def test_tool_error_is_result_not_exception(self) -> None:
        connection = McpConnection(fixture_spec())
        try:
            await connection.open()
            result = await connection.call_tool("boom", {})
            self.assertTrue(bool(result.isError))
        finally:
            await connection.close()

    async def test_open_failure_raises(self) -> None:
        spec = McpServerSpec(name="broken", command="/no/such/command-xyz")
        connection = McpConnection(spec)
        with self.assertRaises(McpConnectionError):
            await connection.open()
        await connection.close()

    async def test_call_after_server_death_raises_connection_error(self) -> None:
        connection = McpConnection(fixture_spec())
        try:
            await connection.open()
            with self.assertRaises(McpConnectionError):
                await connection.call_tool("die", {})
            with self.assertRaises(McpConnectionError):
                await connection.call_tool("echo", {"text": "x"})
        finally:
            await connection.close()
```

（`die` 用例说明：进程退出导致响应流断掉，第一次调用就应转成 `McpConnectionError`；若实现里该次恰好返回了结果，允许放宽为「第二次必抛」——以实现行为为准修断言，但第二次必须抛。）

- [ ] **Step 3: 确认失败**（connection 模块不存在）
- [ ] **Step 4: 实现**（`src/pickel/extensions/mcp/connection.py`）

```python
"""单个 stdio MCP server 的连接生命周期。

stdio_client / ClientSession 的 anyio cancel scope 要求「进入与退出在同一
asyncio 任务」，所以整个 async-with 栈由一个专属背景任务（_run）持有，
open/close 只跟它交换事件。call_tool 可以从任意任务调（SDK 按请求 id 分发）。
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import mcp.types

from pickel.extensions.mcp.config import McpServerSpec

logger = logging.getLogger(__name__)

_OPEN_TIMEOUT_S = 10.0
_CALL_TIMEOUT_S = 60.0


class McpConnectionError(RuntimeError):
    pass


class McpConnection:
    def __init__(self, spec: McpServerSpec) -> None:
        self.spec = spec
        self.tools: list[mcp.types.Tool] = []
        self._session: ClientSession | None = None
        self._ready = asyncio.Event()
        self._shutdown = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._error: BaseException | None = None

    async def open(self) -> None:
        self._runner = asyncio.create_task(
            self._run(), name=f"mcp-connection-{self.spec.name}"
        )
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=_OPEN_TIMEOUT_S)
        except asyncio.TimeoutError:
            await self.close()
            raise McpConnectionError(
                f"MCP server '{self.spec.name}' did not become ready "
                f"within {_OPEN_TIMEOUT_S:.0f}s"
            ) from None
        if self._error is not None:
            raise McpConnectionError(
                f"MCP server '{self.spec.name}' failed to start: {self._error}"
            ) from self._error

    async def _run(self) -> None:
        params = StdioServerParameters(
            command=self.spec.command,
            args=list(self.spec.args),
            env={**os.environ, **self.spec.env},
        )
        try:
            async with stdio_client(params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    self.tools = (await session.list_tools()).tools
                    self._session = session
                    self._ready.set()
                    await self._shutdown.wait()
        except BaseException as exc:
            self._error = exc
        finally:
            self._session = None
            self._ready.set()

    def is_alive(self) -> bool:
        return (
            self._session is not None
            and self._runner is not None
            and not self._runner.done()
        )

    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> mcp.types.CallToolResult:
        if not self.is_alive():
            raise McpConnectionError(
                f"MCP server '{self.spec.name}' is not connected"
            )
        session = self._session
        assert session is not None
        try:
            return await asyncio.wait_for(
                session.call_tool(name, arguments), timeout=_CALL_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            raise
        except Exception as exc:
            # 管道断/进程死的异常形态不可枚举；连接已死就归一成 McpConnectionError
            await asyncio.sleep(0)  # 让 _run 的退出先跑完
            if not self.is_alive():
                raise McpConnectionError(
                    f"connection to MCP server '{self.spec.name}' lost: {exc}"
                ) from exc
            raise

    async def close(self) -> None:
        self._shutdown.set()
        if self._runner is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._runner), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            self._runner.cancel()
            try:
                await self._runner
            except (asyncio.CancelledError, Exception):
                pass
```

- [ ] **Step 5: 通过** `uv run --with pytest --with pytest-asyncio pytest tests/extensions/mcp/ -q`（若 `die` 首调行为与断言不符，按 Step 2 括注修断言）
- [ ] **Step 6: Commit** `git add src/pickel/extensions/mcp/connection.py tests/extensions/mcp && git commit -m "feat(mcp): McpConnection 背景任务持有连接生命周期"`

---

### Task 5: McpProxyTool 与 McpServerRuntime

**Files:**
- Create: `src/pickel/extensions/mcp/proxy.py`
- Create: `src/pickel/extensions/mcp/runtime.py`
- Test: `tests/extensions/mcp/test_runtime.py`

**Interfaces:**
- Consumes: `McpConnection` / `McpConnectionError`（Task 4）、`ExtensionHost.register_mcp_tool` / `unregister_mcp_origin`（Task 2）。
- Produces:
  - `McpServerRuntime(*, spec: McpServerSpec, host)`：`await start()`（连接 + 注册全部工具）；`await call(tool_name: str, arguments: dict) -> mcp.types.CallToolResult`（重连一次语义）；`await close()`；属性 `spec`
  - `McpProxyTool(runtime, tool: mcp.types.Tool)`：`BaseTool` 子类，`spec` 从 MCP 工具直转，`execute` 委托 `runtime.call`

- [ ] **Step 1: 写失败测试**（`tests/extensions/mcp/test_runtime.py`）

```python
from pathlib import Path
import unittest

from pickel.extensions.mcp.runtime import McpServerRuntime
from pickel.extensions_host.host import ExtensionHost
from pickel.extensions_host.registry import ExtensionRegistry
from pickel.tools.base import ToolExecutionContext
from pickel.tools.bus import ToolBus, ToolSource
from pickel.tools.services import ToolServices

from tests.extensions.mcp.test_connection import fixture_spec


def _host(bus: ToolBus) -> ExtensionHost:
    return ExtensionHost(
        name="mcp", config_section=None, tool_bus=bus,
        registry=ExtensionRegistry(), app_config=None,
    )


def _context() -> ToolExecutionContext:
    return ToolExecutionContext(
        agent_id="Pickle", session_id="s", workspace_path=Path("/tmp"),
        services=ToolServices(),
    )


class McpServerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_registers_proxy_tools_on_bus(self) -> None:
        bus = ToolBus()
        runtime = McpServerRuntime(spec=fixture_spec(), host=_host(bus))
        try:
            await runtime.start()
            names = set(bus.list_names(source=ToolSource.MCP))
            self.assertEqual(
                {"mcp__fixture__echo", "mcp__fixture__boom", "mcp__fixture__die"},
                names,
            )
        finally:
            await runtime.close()
        self.assertEqual([], bus.list_names(source=ToolSource.MCP))

    async def test_proxy_execute_converts_text_and_error(self) -> None:
        bus = ToolBus()
        runtime = McpServerRuntime(spec=fixture_spec(), host=_host(bus))
        try:
            await runtime.start()
            echo = bus.get("mcp__fixture__echo").tool
            result = await echo.execute({"text": "hi"}, _context())
            self.assertFalse(result.is_error)
            self.assertEqual("echo:hi", result.content)
            self.assertEqual("fixture", result.metadata["server"])

            boom = bus.get("mcp__fixture__boom").tool
            result = await boom.execute({}, _context())
            self.assertTrue(result.is_error)
        finally:
            await runtime.close()

    async def test_proxy_marks_non_text_content_as_unsupported(self) -> None:
        import mcp.types
        from types import SimpleNamespace

        from pickel.extensions.mcp.proxy import McpProxyTool

        async def fake_call(tool_name, arguments):
            return mcp.types.CallToolResult(
                content=[
                    mcp.types.TextContent(type="text", text="hello"),
                    mcp.types.ImageContent(
                        type="image", data="aGk=", mimeType="image/png"
                    ),
                ],
                isError=False,
            )

        runtime = SimpleNamespace(
            call=fake_call, spec=SimpleNamespace(name="fake")
        )
        tool = mcp.types.Tool(
            name="t", description="d", inputSchema={"type": "object"}
        )
        result = await McpProxyTool(runtime, tool).execute({}, _context())
        self.assertEqual("hello\n[unsupported content: image]", result.content)
        self.assertEqual(["image"], result.metadata["unsupported_content"])

    async def test_call_reconnects_after_server_death(self) -> None:
        bus = ToolBus()
        runtime = McpServerRuntime(spec=fixture_spec(), host=_host(bus))
        try:
            await runtime.start()
            die = bus.get("mcp__fixture__die").tool
            first = await die.execute({}, _context())
            self.assertTrue(first.is_error)

            echo = bus.get("mcp__fixture__echo").tool
            second = await echo.execute({"text": "back"}, _context())
            self.assertFalse(second.is_error)
            self.assertEqual("echo:back", second.content)
        finally:
            await runtime.close()

    async def test_reconnect_failure_unregisters_server_tools(self) -> None:
        bus = ToolBus()
        spec = fixture_spec()
        runtime = McpServerRuntime(spec=spec, host=_host(bus))
        try:
            await runtime.start()
            die = bus.get("mcp__fixture__die").tool
            echo = bus.get("mcp__fixture__echo").tool
            await die.execute({}, _context())
            # 让重连必然失败：偷换 spec 为坏命令
            runtime.spec = type(spec)(
                name=spec.name, command="/no/such/command-xyz",
            )
            result = await echo.execute({"text": "x"}, _context())
            self.assertTrue(result.is_error)
            self.assertEqual([], bus.list_names(source=ToolSource.MCP))
        finally:
            await runtime.close()
```

（`runtime.spec` 需为普通属性以便测试偷换——实现时不要用 frozen dataclass 包 runtime。`die` 的 execute 结果：第一次调用死亡触发重连、对 `die` 重试又死、二次即失败 → is_error；重试不递归的语义正好被它覆盖。）

- [ ] **Step 2: 确认失败**
- [ ] **Step 3: 实现**

`src/pickel/extensions/mcp/proxy.py`：

```python
"""MCP 工具 → BaseTool 代理。schema 直传，结果拍平为文本。"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import mcp.types

from pickel.tools.base import BaseTool, ToolExecutionContext, ToolExecutionResult, ToolSpec

if TYPE_CHECKING:
    from pickel.extensions.mcp.runtime import McpServerRuntime


class McpProxyTool(BaseTool):
    def __init__(self, runtime: "McpServerRuntime", tool: mcp.types.Tool) -> None:
        self.spec = ToolSpec(
            name=tool.name,
            description=tool.description or "",
            input_schema=tool.inputSchema,
        )
        self._runtime = runtime

    async def execute(
        self,
        arguments: dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolExecutionResult:
        metadata: dict[str, Any] = {
            "server": self._runtime.spec.name,
            "mcp_tool": self.spec.name,
        }
        try:
            result = await self._runtime.call(self.spec.name, arguments)
        except asyncio.TimeoutError:
            return ToolExecutionResult(
                content=f"MCP tool call timed out ({self.spec.name})",
                is_error=True,
                metadata=metadata,
            )
        except Exception as exc:
            return ToolExecutionResult(
                content=(
                    f"MCP server '{self._runtime.spec.name}' is unavailable: {exc}"
                ),
                is_error=True,
                metadata=metadata,
            )
        parts: list[str] = []
        unsupported: list[str] = []
        for block in result.content:
            if isinstance(block, mcp.types.TextContent):
                parts.append(block.text)
            else:
                parts.append(f"[unsupported content: {block.type}]")
                unsupported.append(block.type)
        if unsupported:
            metadata["unsupported_content"] = unsupported
        return ToolExecutionResult(
            content="\n".join(parts),
            is_error=bool(result.isError),
            metadata=metadata,
        )
```

`src/pickel/extensions/mcp/runtime.py`：

```python
"""单个 MCP server 的运行时编排：注册、调用、重连一次、卸载。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import mcp.types

from pickel.extensions.mcp.config import McpServerSpec
from pickel.extensions.mcp.connection import McpConnection, McpConnectionError
from pickel.extensions.mcp.proxy import McpProxyTool

logger = logging.getLogger(__name__)


class McpServerRuntime:
    def __init__(self, *, spec: McpServerSpec, host: Any) -> None:
        self.spec = spec
        self._host = host
        self._connection: McpConnection | None = None
        self._reconnect_lock = asyncio.Lock()

    async def start(self) -> None:
        self._connection = McpConnection(self.spec)
        await self._connection.open()
        self._register_tools()

    async def call(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> mcp.types.CallToolResult:
        connection = self._connection
        if connection is None or not connection.is_alive():
            await self._reconnect()
            connection = self._connection
            assert connection is not None
        try:
            return await connection.call_tool(tool_name, arguments)
        except McpConnectionError:
            await self._reconnect()
            assert self._connection is not None
            # 重试恰好一次；这里再失败就任由异常出去（proxy 转 is_error）
            return await self._connection.call_tool(tool_name, arguments)

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
            self._connection = None
        self._host.unregister_mcp_origin(self.spec.name)

    def _register_tools(self) -> None:
        # 先卸后注：server 升级后消失的工具被剔除
        self._host.unregister_mcp_origin(self.spec.name)
        assert self._connection is not None
        for tool in self._connection.tools:
            self._host.register_mcp_tool(
                McpProxyTool(self, tool), server=self.spec.name
            )

    async def _reconnect(self) -> None:
        async with self._reconnect_lock:
            if self._connection is not None and self._connection.is_alive():
                return  # 并发失败的其他调用已经重连好了
            logger.warning("Reconnecting to MCP server '%s'", self.spec.name)
            if self._connection is not None:
                await self._connection.close()
                self._connection = None
            connection = McpConnection(self.spec)
            try:
                await connection.open()
            except McpConnectionError:
                self._host.unregister_mcp_origin(self.spec.name)
                raise
            self._connection = connection
            self._register_tools()
```

- [ ] **Step 4: 通过** `uv run --with pytest --with pytest-asyncio pytest tests/extensions/mcp/ -q`
- [ ] **Step 5: Commit** `git add src/pickel/extensions/mcp tests/extensions/mcp && git commit -m "feat(mcp): 代理工具与 server 运行时（注册/调用/重连一次）"`

---

### Task 6: setup / teardown 组装

**Files:**
- Modify: `src/pickel/extensions/mcp/__init__.py`（替换 Task 3 的占位）
- Test: `tests/extensions/mcp/test_setup.py`

**Interfaces:**
- Consumes: `load_mcp_servers`（Task 3）、`McpServerRuntime`（Task 5）、`host.config` / `host.app_config`（Task 2）、`pickel.config.paths.home_dir`。
- Produces: extension 入口 `async setup(host)`、`async teardown()`；配置模型 `McpExtensionConfig(enabled: bool = True)`。

- [ ] **Step 1: 写失败测试**（`tests/extensions/mcp/test_setup.py`）

```python
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest import mock

import pickel.extensions.mcp as mcp_extension
from pickel.extensions_host.host import ExtensionHost
from pickel.extensions_host.registry import ExtensionRegistry
from pickel.tools.bus import ToolBus, ToolSource

from tests.extensions.mcp.test_connection import FIXTURE


def _mcp_json(root: Path, servers: dict) -> None:
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8"
    )


def _host(bus: ToolBus, root: Path, section: dict | None = None) -> ExtensionHost:
    return ExtensionHost(
        name="mcp", config_section=section, tool_bus=bus,
        registry=ExtensionRegistry(), app_config=SimpleNamespace(root=root),
    )


def _fixture_entry() -> dict:
    import sys
    return {"command": sys.executable, "args": [str(FIXTURE)]}


class McpSetupTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_connects_and_registers_then_teardown_unregisters(self) -> None:
        bus = ToolBus()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mcp_json(root, {"fixture": _fixture_entry()})
            with mock.patch.object(mcp_extension, "home_dir", return_value=root / "nohome"):
                await mcp_extension.setup(_host(bus, root))
            try:
                self.assertIn(
                    "mcp__fixture__echo", bus.list_names(source=ToolSource.MCP)
                )
            finally:
                await mcp_extension.teardown()
        self.assertEqual([], bus.list_names(source=ToolSource.MCP))

    async def test_failing_server_is_isolated(self) -> None:
        bus = ToolBus()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mcp_json(root, {
                "broken": {"command": "/no/such/command-xyz"},
                "fixture": _fixture_entry(),
            })
            with mock.patch.object(mcp_extension, "home_dir", return_value=root / "nohome"):
                await mcp_extension.setup(_host(bus, root))
            try:
                names = bus.list_names(source=ToolSource.MCP)
                self.assertIn("mcp__fixture__echo", names)
                self.assertEqual(
                    [], [n for n in names if n.startswith("mcp__broken__")]
                )
            finally:
                await mcp_extension.teardown()

    async def test_disabled_via_settings_registers_nothing(self) -> None:
        bus = ToolBus()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _mcp_json(root, {"fixture": _fixture_entry()})
            with mock.patch.object(mcp_extension, "home_dir", return_value=root / "nohome"):
                await mcp_extension.setup(_host(bus, root, section={"enabled": False}))
            self.assertEqual([], bus.list_names(source=ToolSource.MCP))

    async def test_no_mcp_json_is_silent(self) -> None:
        bus = ToolBus()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(mcp_extension, "home_dir", return_value=root / "nohome"):
                await mcp_extension.setup(_host(bus, root))
            self.assertEqual([], bus.list_names(source=ToolSource.MCP))
```

- [ ] **Step 2: 确认失败**（占位 setup 不注册任何东西 → 第一个用例 FAIL）
- [ ] **Step 3: 实现**（替换 `src/pickel/extensions/mcp/__init__.py`）

```python
"""MCP 客户端 extension：把 stdio MCP server 的工具接入工具总线。

server 列表读 .mcp.json（~/.pickel/ 全局 + workspace 项目级，项目覆盖同名）；
extension 启停沿用 settings.json 的 extensions.mcp.enabled（默认开）。
单 server 失败隔离：记 warning 跳过，不阻断启动。
"""

from __future__ import annotations

import logging

from pydantic import BaseModel

from pickel.config.paths import home_dir
from pickel.extensions.mcp.config import load_mcp_servers
from pickel.extensions.mcp.runtime import McpServerRuntime

logger = logging.getLogger(__name__)

_runtimes: list[McpServerRuntime] = []


class McpExtensionConfig(BaseModel):
    enabled: bool = True


async def setup(host) -> None:
    config = host.config(McpExtensionConfig)
    if config is not None and not config.enabled:
        return
    project_root = getattr(host.app_config, "root", None)
    if project_root is None:
        logger.warning("MCP extension: app_config.root unavailable; skipping")
        return
    specs = load_mcp_servers(home=home_dir(), project_root=project_root)
    for spec in specs.values():
        runtime = McpServerRuntime(spec=spec, host=host)
        try:
            await runtime.start()
        except Exception:
            logger.warning(
                "MCP server '%s' failed to start; skipping", spec.name, exc_info=True
            )
            continue
        _runtimes.append(runtime)
        logger.info(
            "MCP server '%s' connected (%d tools)",
            spec.name,
            len(runtime._connection.tools) if runtime._connection else 0,
        )


async def teardown() -> None:
    for runtime in _runtimes:
        try:
            await runtime.close()
        except Exception:
            logger.exception("MCP server '%s' close failed", runtime.spec.name)
    _runtimes.clear()
```

- [ ] **Step 4: 通过** `uv run --with pytest --with pytest-asyncio pytest tests/extensions/ tests/extensions_host/ -q`
- [ ] **Step 5: Commit** `git add src/pickel/extensions/mcp tests/extensions/mcp && git commit -m "feat(mcp): extension 装配——setup 全连失败隔离，teardown 全卸"`

---

### Task 7: 全量测试、手动验收与设计稿校对

**Files:**
- Modify: `docs/upgrade/2026-07-26-mcp-client-design.md`（按实施补两处）

- [ ] **Step 1: 全量测试**

```bash
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | tail -3
uv run --with pytest --with pytest-asyncio pytest -q 2>&1 | grep FAILED | sed 's/::.*//' | sort | uniq -c
```

Expected: 失败分布 = 基线 12 例（tests/app/test_assembly.py 3、tests/cli/test_chat_loop.py 1、tests/providers/test_gemini.py 7、tests/providers/test_model_context_generate.py 1，全缺 key）。

- [ ] **Step 2: 手动验收**（真实 chat 会话）

workspace 根写 `.mcp.json`（用 fixture server 即可）：

```json
{"mcpServers": {"fixture": {"command": "<python 路径>", "args": ["<绝对路径>/tests/extensions/mcp/fixture_server.py"]}}}
```

`agents/Pickle/agent.yaml` 临时加一行 `- mcp__fixture__*`，然后：

```bash
set -a; . ~/.pickel/.env; set +a; uv run pickel chat
```

- 问模型「用 mcp__fixture__echo 回显 hello」→ 工具出现在可用列表、调用成功返回 `echo:hello`
- `/reload` → 工具仍在（teardown 重连）
- 验收后还原 agent.yaml 与 `.mcp.json`（不提交临时改动）

- [ ] **Step 3: 设计稿校对**（`docs/upgrade/2026-07-26-mcp-client-design.md`）

- §3.1 试金石结论补第二条发现：`ExtensionHost` 原本不暴露 `app_config`，mcp 需要项目根定位 `.mcp.json`，已增 `host.app_config`。
- §3.3 补：单次工具调用超时 60s（asyncio.wait_for），超时转 is_error 不触发重连。
- 核对 §5 错误表与实现一致，不一致处按实现改。

- [ ] **Step 4: Commit**

```bash
git add docs/upgrade/2026-07-26-mcp-client-design.md
git commit -m "docs(mcp): 设计稿按实施校对（app_config 试金石、调用超时）"
```
