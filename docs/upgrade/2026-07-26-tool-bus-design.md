# T1：工具总线与热插拔 —— 设计稿

调研依据：[2026-07-26-tools-sandbox-research.md](2026-07-26-tools-sandbox-research.md)

日期：2026-07-26 · 分支：`feat/tools-and-sandbox`

---

## 1. 目标与范围

把 `ToolRegistry` 从静态查找表改造为可变工具总线，使工具的注册、启停、来源分层、版本标识成为运行期能力，并把「工具集在 `Run` 生命周期内冻结」改为「turn 边界快照」。

T1 是 T2（MCP + extension 接入）与 V1（版本管理）的地基，本身不引入任何新工具来源。

| 做 | 不做 |
| --- | --- |
| `ToolBus` 替代 `ToolRegistry`：可变、来源分层、启停、版本字段、列举 | MCP 客户端实现（T2） |
| 注册表与激活集分离；激活集三层求交 | extension 装载器（T2） |
| turn 边界快照；prepare 与 react 共用同一份 | 沙箱与进程隔离（S2） |
| `ToolExecutionContext` 的 `Any` 字段换成强类型 `ToolServices` | skill 版本与审批（V1） |
| `tool_set_active` 内置工具 | 工具参数 schema 演进 shim（V1） |
| bus 跨 `/reload` 存活 | 工具级权限策略（S2） |

---

## 2. 架构

### 2.1 三个概念的分离

现状把三件事压成了一个静态 list：

```
现状：builtin_tools() ──► ToolRegistry(dict) ──► resolve_many(agent.tool_ids) ──► Run.tools (list, 冻结)
```

T1 拆成三层，各自有独立的可变性与生命周期：

| 层 | 回答的问题 | 生命周期 | 可变性 |
| --- | --- | --- | --- |
| **注册表**（`ToolBus`） | 系统里存在哪些工具 | 进程级，跨 `Run` / session / `/reload` | 随时可变（`register` / `unregister` / `set_enabled`） |
| **激活集**（`ToolActivation`） | 这一 turn 暴露哪些给模型 | turn 级 | 每 turn 重算 |
| **快照**（`ToolSnapshot`） | 这一 turn 实际用的是哪些工具对象 | turn 级，**turn 内不可变** | 只读 |

### 2.2 激活集的三层求交

```
agent.yaml 的 tools 白名单        人的授权，硬边界，agent 无法扩张
        ∩
bus 中 enabled 的条目             运维/故障隔离开关
        ∩
agent 运行时未通过 tool_set_active 关闭的    agent 的自我收窄
        =
        本 turn 激活集
```

白名单在最外层，意味着 `tool_set_active` 只能收窄、永不扩张 —— 这是「agent 只能启停、不能装卸」的机制保证，而不是靠提示词约束。

### 2.3 快照落在 turn 而非 step

`ReActStrategy.execute` 里 `TurnState` 建于 turn 开始（`runs/strategy/react.py:56`），而 `prepare` 在**每个 step** 都调用一次（`react.py:62-72` 的循环内）。因此快照必须挂在 `TurnState` 上、由 turn 内所有 step 共用：

- 工具定义是 system 之后第一段稳定内容。每 step 重算会让 prompt cache 每 step 失效。
- 模型看到的定义与 react 执行时找到的工具对象必须是同一份，否则可能出现「模型按旧定义调用、执行到新实现」。

**语义后果**：agent 在第 3 步调用 `tool_set_active` 关掉某工具，本 turn 剩余步骤仍看得见它，**下一 turn 生效**。这是有意为之 —— 与调研结论「热插拔必须落在 turn 边界」一致（Hermes 把「缓存兼容性：禁止对话中途变更」列为独立门控）。

### 2.4 bus 的持有者

`/reload` 会重建 `Boot`（`cli/chat.py:424`），所以 bus **不能挂在 `Boot` 上**，否则每次 reload 都会丢掉 T2 引入的 MCP 子进程。

持有链：

```
ChatApp._tool_bus  (进程级，跨 reload 存活)
      │ 注入
      ▼
Boot.from_config(app_config, tool_bus=bus)
      │ 注入
      ▼
Run.open(..., tool_bus=bus)
      │ 引用（不拥有）
      ▼
Run.tool_bus
```

`Run.reload` 把旧 run 的 bus 引用传给新 run，只重算激活集，不触碰注册表。

---

## 3. 组件与接口

### 3.1 `tools/bus.py`（新）

```python
class ToolSource(StrEnum):
    BUILTIN = "builtin"
    MCP = "mcp"
    EXTENSION = "extension"


@dataclass(frozen=True)
class ToolEntry:
    name: str                         # 最终名（含命名空间前缀），全 bus 唯一
    tool: BaseTool
    source: ToolSource
    version: str | None = None        # T1 内置工具恒为 None；V1 填 git SHA / semver
    origin: str | None = None         # 来源标识：MCP server 名 / extension 目录名
    enabled: bool = True


class ToolBus:
    """进程级工具注册表。可变，跨 Run / session / reload 存活。"""

    def register(self, tool: BaseTool, *, source: ToolSource, version: str | None = None,
                 origin: str | None = None) -> None: ...
    def unregister(self, name: str) -> None: ...
    def unregister_origin(self, source: ToolSource, origin: str) -> list[str]:
        """卸掉某个来源的全部工具，返回被卸的名字。T2 用于 MCP server 断开。"""
    def set_enabled(self, name: str, enabled: bool) -> None: ...
    def get(self, name: str) -> ToolEntry: ...          # KeyError if unknown
    def list(self, *, source: ToolSource | None = None) -> list[ToolEntry]: ...
    def snapshot(self, activation: ToolActivation) -> ToolSnapshot: ...
```

`register` 同名冲突处理：同 `source` + 同 `origin` 视为重新注册（覆盖，用于 T2 的 MCP 重连）；跨 `source` 同名直接 `ToolNameConflictError` —— 命名空间前缀（3.2）已保证跨来源不会撞名，撞了就是 bug，不静默覆盖。

### 3.2 命名空间

| 来源 | 工具名形态 | 例 |
| --- | --- | --- |
| builtin | 裸名 | `read_file` |
| mcp | `mcp__<server>__<tool>` | `mcp__github__create_issue` |
| extension | `mcp__<ext>__<tool>` | `mcp__my_tool__run` |

extension 收敛为本地 stdio MCP server（见调研结论），因此与 MCP 共用一套前缀，`ToolSource` 仍区分二者用于列举与信任分级。

前缀由 bus 在 `register` 时按 `source` + `origin` 计算，结果写入 `ToolEntry.name`。工具自身的 `spec.name` 保持裸名不变（工具不需要知道自己被挂在什么命名空间下）。

**因此 `ToolEntry.name` 是全链路的唯一工具标识**：`agent.yaml` 白名单、`ToolSnapshot.definitions()` 给模型的 `ToolDefinition.name`、模型回传的 `tool_call.name`、`ToolSnapshot.find()` 的查找键，全部用它，而非 `tool.spec.name`。内置工具二者恰好相等，非内置工具不等 —— 实施时不可混用。

### 3.3 `tools/activation.py`（新）

```python
@dataclass(frozen=True)
class ToolActivation:
    """一次 turn 的激活集计算输入。"""

    allowed: frozenset[str]                  # agent.yaml 的 tools 白名单，硬边界
    agent_disabled: frozenset[str] = frozenset()   # agent 通过 tool_set_active 关闭的

    def with_agent_disabled(self, names: Iterable[str]) -> ToolActivation: ...
```

```python
@dataclass(frozen=True)
class ToolSnapshot:
    """turn 内不可变的工具视图。prepare 与 react 的唯一来源。"""

    entries: tuple[ToolEntry, ...]

    def definitions(self) -> list[ToolDefinition]: ...   # 给 prepare
    def find(self, name: str) -> BaseTool | None: ...    # 给 react
    @property
    def names(self) -> tuple[str, ...]: ...
```

`ToolBus.snapshot(activation)` 的计算：

```
entries = [e for e in bus.list()
           if e.enabled
           and e.name in activation.allowed
           and e.name not in activation.agent_disabled]
```

白名单里存在但 bus 中不存在的名字 → 不报错，记一条 warning 事件（T2 之后是常态：配置引用了尚未连上的 MCP server 的工具）。

### 3.4 `tools/services.py`（新）

```python
if TYPE_CHECKING:                    # 运行期不导入，避免 base ↔ shell / file_service 循环
    from pickel.tools.file_service import WorkspaceFileService
    from pickel.tools.shell import ShellSessionManager


@dataclass(frozen=True)
class ToolServices:
    """进程内工具可用的运行期服务。"""

    workspace_files: "WorkspaceFileService | None" = None
    shell_sessions: "ShellSessionManager | None" = None
```

`ToolExecutionContext` 相应改为：

```python
@dataclass(frozen=True)
class ToolExecutionContext:
    agent_id: str
    session_id: str
    workspace_path: Path
    services: ToolServices = field(default_factory=ToolServices)
```

消费点只有两个 helper：`tools/file_tools.py:12-14` 的 `_require_workspace_files`、`tools/shell.py:455` 的 `_require_shell_manager`，各改一行为 `context.services.*`。

不做「能力声明 + 按需注入」：extension 收敛到 MCP 后，进程内工具只剩内置工具（我们自己写），一个字段明确的 dataclass 就够，多一层间接只增加理解成本。

### 3.5 `tool_set_active` 内置工具

```
name: tool_set_active
input: { disable?: string[], enable?: string[] }
```

语义：把名字加入 / 移出 `TurnState` 的 `agent_disabled` 集合，**下一 turn 生效**（返回值明说这一点，避免模型以为立即生效后困惑）。`enable` 只能移出 `agent_disabled`，无法把白名单外的工具拉进来 —— 尝试时返回错误说明该工具未被授权。

`agent_disabled` 需要跨 turn 存活，因此它是 `Run.activation` 的一部分（会话级），而非 `TurnState` 的字段；`TurnState` 只持有本 turn 的快照。写入方式：

```python
run.activation = run.activation.with_agent_disabled(names)
```

`Run` 上只有 `activation` 这一个激活状态字段，不额外持有 `agent_disabled` 集合 —— 避免两处状态需要同步。

### 3.6 `catalog.py` 的角色变化

`builtin_tools()` 保留，但从「被 `Run.open` 直接调用」变成「被 bus 装配函数调用一次」：

```python
def install_builtin_tools(bus: ToolBus) -> None:
    for tool in builtin_tools():
        bus.register(tool, source=ToolSource.BUILTIN)
```

---

## 4. 数据流

```
进程启动
   ChatApp 构造 ToolBus → install_builtin_tools(bus)
   Boot.from_config(app_config, tool_bus=bus)
   Run.open(..., tool_bus=bus)   # Run 持引用，不拥有
        └─ Run.activation = ToolActivation(allowed=frozenset(agent.tool_ids))

turn 开始（ReActStrategy.execute）
   turn = TurnState()
   turn.tool_snapshot = run.tool_bus.snapshot(run.activation)      ← 唯一快照点
   │
   ├─ step 1..N:
   │     prepare(run=run, turn=turn)
   │        └─ resolve_tools → turn.tool_snapshot.definitions()    ← 不再读 run.tools
   │     provider.generate(...)
   │     _execute_tool_call
   │        └─ turn.tool_snapshot.find(tool_call.name)             ← 不再遍历 run.tools
   │     （tool_set_active 只写 run.agent_disabled，本 turn 快照不动）
   │
   turn 结束

/reload
   ChatApp 复用同一个 bus → Boot.from_config(app_config, tool_bus=self._tool_bus)
   Run.reload → 新 Run 持同一 bus，重算 activation（白名单可能因配置改变）
```

---

## 5. 改动清单

| 文件 | 改动 |
| --- | --- |
| `tools/bus.py` | 新增：`ToolSource`、`ToolEntry`、`ToolBus`、`ToolNameConflictError` |
| `tools/activation.py` | 新增：`ToolActivation`、`ToolSnapshot` |
| `tools/services.py` | 新增：`ToolServices` |
| `tools/registry.py` | 删除（`ToolRegistry` 无外部使用者，仅 `Run.open` 与 `tools/__init__` 导出） |
| `tools/base.py` | `ToolExecutionContext`：`workspace_files` / `shell_session_manager: Any` → `services: ToolServices` |
| `tools/catalog.py` | 增 `install_builtin_tools(bus)` |
| `tools/builtin.py` | 增 `tool_set_active` |
| `tools/file_tools.py` | `_require_workspace_files` 改读 `context.services.workspace_files` |
| `tools/shell.py` | `_require_shell_manager` 改读 `context.services.shell_sessions` |
| `tools/__init__.py` | 导出调整 |
| `runs/run.py` | `tools: list[BaseTool]` → `tool_bus: ToolBus` + `activation: ToolActivation`（`agent_disabled` 在 activation 内）；`open()` 接受 `tool_bus` 注入而非自建；`get_tool_execution_context` 组装 `ToolServices`；`reload` 传递 bus |
| `runs/turn_state.py` | `TurnState` 增 `tool_snapshot: ToolSnapshot \| None = None` |
| `runs/strategy/react.py` | turn 开始取快照存入 `turn.tool_snapshot`；`_execute_tool_call` 改用 `turn.tool_snapshot.find(tool_call.name)`；调 prepare 时传 `snapshot=turn.tool_snapshot` |
| `context/prepare.py` | `resolve_tools(*, run)` → `resolve_tools(*, snapshot: ToolSnapshot)`；`prepare()` 增 `snapshot` 关键字参数（不传整个 `TurnState`，prepare 只需要快照） |
| `app/boot.py` | `from_config` / `__init__` 接受 `tool_bus`；`build_run` 透传 |
| `cli/chat.py` | 持有 `self._tool_bus`；`/reload` 复用；`/reload` 帮助文本更新 |

`config.yaml` 与 `agents/*/agent.yaml` 的 `tools` 白名单语义不变，**零迁移**。

---

## 6. 错误处理

| 情形 | 行为 |
| --- | --- |
| 白名单引用了 bus 中不存在的工具 | 不报错，跳过 + warning 事件（T2 后为常态） |
| 跨 source 注册同名工具 | `ToolNameConflictError`，注册方负责处理 |
| 同 source + 同 origin 重复注册 | 覆盖（T2 的 MCP 重连路径） |
| 模型调用了快照外的工具名 | 沿用现状：返回 `Tool 'x' is not available.` 错误结果，不中断 turn |
| `tool_set_active` 试图 enable 白名单外的工具 | 错误结果，说明未被授权 |
| `ToolServices` 字段为 `None` 但工具需要 | 沿用现状 helper 的 `RuntimeError` |
| 快照为空（白名单与 bus 无交集） | 允许，`tools=[]`，模型进入纯文本模式 |

---

## 7. 测试计划

| 层 | 用例 |
| --- | --- |
| `ToolBus` | register / unregister / unregister_origin / set_enabled / list(source=…)；同 source 同 origin 覆盖；跨 source 同名冲突抛错 |
| 激活集 | 三层求交；白名单外的名字被 `agent_disabled` 忽略；enable 无法越过白名单；bus 缺失的白名单项被跳过 |
| 快照不变性 | turn 内改 bus（register / set_enabled）不影响已取快照；`definitions()` 与 `find()` 对同一 entry 一致 |
| turn 边界语义 | `tool_set_active` 在 step 3 调用 → 本 turn 后续 step 的 `prepare` 输出不变；下一 turn 的快照生效 |
| `ToolServices` | 现有 file / shell 工具测试改造后仍通过（回归） |
| bus 跨 reload | `Run.reload` 后 bus 是同一实例；已注册的非 builtin 条目仍在（用假条目模拟 T2） |
| prepare | `resolve_tools(snapshot=…)` 输出与旧 `resolve_tools(run=…)` 对同一工具集等价（防回归） |

已知失败测试 `tests/tools/test_shell.py` 的 6 例（ANSI 转义）属 S1，不在 T1 修复范围；T1 的改动不应改变其失败原因。

---

## 8. 为 T2 / V1 预留的接口

| 后续项 | T1 已备好的挂点 |
| --- | --- |
| T2 MCP 接入 | `ToolSource.MCP`、命名空间前缀、`unregister_origin`（server 断开时批量卸载）、bus 跨 reload 存活 |
| T2 extension | `ToolSource.EXTENSION` + 同一套 MCP 前缀 |
| V1 版本管理 | `ToolEntry.version` / `origin` 字段已在，等待填充 |
| S2 沙箱 | `ToolServices` 是工具获取运行期能力的唯一入口，沙箱化的 shell / 文件服务从这里替换即可 |

---

## 9. 遗留取舍

1. **bus 进程级共享 → 跨 agent 隔离靠激活集，而非子进程隔离。** 同一 MCP server 配置的多个 agent 共用一个子进程。真正的跨 agent 进程隔离归 S2。
2. **`tool_set_active` 下一 turn 生效**，模型可能在同一 turn 内重复尝试。工具返回值需明确说明生效时机。
3. **`ToolEntry.version` 在 T1 恒为 `None`**（内置工具没有独立版本，跟随仓库 git 历史）。字段先在，V1 才有填充逻辑。
