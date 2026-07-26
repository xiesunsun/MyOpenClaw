# E1：Extension 宿主 —— 设计稿

调研依据：[2026-07-26-tools-sandbox-research.md](2026-07-26-tools-sandbox-research.md)（§1.4 已有扩展点、§3.1 extension 不是工具的一种打包形式）
前置：[T1 工具总线](2026-07-26-tool-bus-design.md)

日期：2026-07-26 · 分支：`feat/tools-and-sandbox`

---

## 1. 目标与范围

给 harness 装上插件宿主：第三方（以及我们自己）可以在不改 core 的前提下，向工具、hook 生命周期、上下文召回、会话同步四个位点贡献实现。

**验收标准：把 openviking 从 core 里完整摘出去** —— `app/boot.py` 的三段硬编码清空、`config/app_config.py` 不再 import openviking 的类型，1061 行集成代码原地变成一个走 extension API 的内置 extension，功能与配置行为不变。

| 做 | 不做 |
| --- | --- |
| `ExtensionHost` API：`register_tool` / `add_hook_handler` / `add_recall_source` / `add_session_sync` | `register_command`（chat.py 的 if/elif 链重构）→ E2 |
| 内置（`src/pickel/extensions/`）与用户级（`~/.pickel/extensions/`）两处发现与装载 | 项目级 `.pickel/extensions/` 与信任门 → E2 |
| `extensions:` 配置段；extension 自解析自己的配置模型 | `add_skill_path`、`register_provider`、compaction hook、`input` 拦截、UI 渲染器 → E2 |
| 把 `LifecycleHooks` 接进生产路径（现在是死的） | extension 的打包、分发、版本约束 → V1 |
| `SessionSync` Protocol 移到 core + `CompositeSessionSync` | extension 沙箱化（第三方代码跑在本进程内）→ S2 |
| openviking 改造为内置 extension | 装载后的运行时动态注册（Pi 的 `session_start` 内注册）→ E2 |

---

## 2. 两处必须先解掉的耦合

写稿时核对代码，发现两处比预想更深的反向依赖 —— core 依赖了集成层，不解掉 extension 化无从谈起：

| 位置 | 现状 | 问题 | E1 的处理 |
| --- | --- | --- | --- |
| `config/app_config.py:9` | `from pickel.integrations.openviking.config import OpenVikingConfig`，且 `AppConfig.openviking: OpenVikingConfig \| None` | **core 的配置模型静态引用了集成层的类型。** 只要这行还在，openviking 就永远搬不走 | `AppConfig.extensions: dict[str, dict[str, Any]]` —— core 只存原始 dict，不认识任何 extension 的配置模型；解析由 extension 自己做（`host.config(Model)`） |
| `integrations/openviking/session_sync.py:28` | `SessionSync` Protocol 与 `NoopSessionSync` 定义在 openviking 集成里，`conversations/service.py` 从这里 import | **core 的会话服务从集成层 import 自己的协议。** | Protocol 与 Noop 移到 `conversations/session_sync.py`（core），只有 `OpenVikingSessionSync` 留在 extension |

`Recall` Protocol 已经在 core（`context/recall.py`），方向正确，不动。

---

## 3. 架构

### 3.1 三个角色

| 角色 | 职责 | 位置 |
| --- | --- | --- |
| `ExtensionHost` | 给 extension 用的宿主 API。extension 只认识它 | `extensions_host/host.py` |
| `ExtensionRegistry` | 宿主内部收集到的贡献；`Boot` 从这里取 | `extensions_host/registry.py` |
| `ExtensionLoader` | 发现目录、import 模块、调 `setup()`、隔离失败 | `extensions_host/loader.py` |

**`ExtensionHost` 是 per-extension 实例**：loader 为每个 extension 单独构造一个，绑定它的名字、它那段配置、共享的 `ToolBus` 与 `ExtensionRegistry`。因此 extension 不需要（也无法）自报名字 —— `host.config(Model)` 取的是 `app_config.extensions[<本 extension 名>]`，`host.register_tool(t)` 落的 `origin` 就是本 extension 名。名字由发现时的目录名决定，是 extension 的身份，不可自定义。

包名用 `extensions_host`（core 侧宿主），与 `extensions/`（内置 extension 存放处）区分开，避免 `pickel.extensions.openviking` 与 `pickel.extensions.host` 混在一个命名空间里语义打结。

### 3.2 extension 的形态

一个 Python 包或单文件模块，暴露模块级 `setup` 函数：

```python
# src/pickel/extensions/openviking/__init__.py
def setup(host: ExtensionHost) -> None:
    config = host.config(OpenVikingConfig)      # 解析自己那段配置
    if config is None or not config.enabled:
        return                                  # 未启用：什么都不注册
    host.add_recall_source(_make_recall)        # 注册工厂，不是实例
    host.add_session_sync(_make_session_sync)
```

`setup` 可以是 `async def`（宿主 await 它），以支持需要异步初始化的 extension（T2 的 MCP 客户端要连子进程）。

**纪律（照搬 Pi）：不要在 `setup` 里启长驻资源**（watcher、timer、子进程、长连接）。`setup` 只做注册。需要长驻资源的，在工厂被调用时创建、在 `teardown` 里释放：

```python
async def teardown(host: ExtensionHost) -> None:    # 可选
    ...
```

### 3.3 per-agent 工厂 vs 进程级实例

这是本设计最关键的一处判断。核对现状后发现：`recall_sources` 与 `session_sync` 的构造**都依赖 agent_id**（openviking 按 agent 解析 `remote_agent_id`，见 `boot._resolve_openviking_remote_agent_id`），而工具是进程级的。所以两类扩展点的注册形态不同：

| 扩展点 | 注册什么 | 何时求值 | 理由 |
| --- | --- | --- | --- |
| `register_tool(tool)` | **实例** | 装载时立即进 `ToolBus`（`source=EXTENSION`、`origin=<extension 名>`） | 工具是进程级的，`ToolBus` 跨 Run / session 存活（T1） |
| `add_hook_handler(factory)` | **工厂** `(AgentScope) -> handler \| None` | `Boot.build_run(agent_id)` | hook handler 现在就是 per-Run 注入的 |
| `add_recall_source(factory)` | **工厂** `(AgentScope) -> Recall \| None` | `Boot.build_run(agent_id)` | 对应现状 `_build_recall_sources(agent_id=...)` |
| `add_session_sync(factory)` | **工厂** `(AgentScope) -> SessionSync \| None` | `Boot.build_session_service(agent_id)` | 对应现状 `_build_session_sync(agent_id=...)` |

工厂返回 `None` = 这个 agent 不启用该贡献。这正好承接现状里那一堆 `if not enabled: return NoopSessionSync()` / `return []` 的分支逻辑 —— 判断搬进 extension，core 只负责过滤掉 `None`。

```python
@dataclass(frozen=True)
class AgentScope:
    """工厂求值时的 agent 上下文。"""

    agent_id: str
    app_config: AppConfig
```

`AppConfig` 是 core 的公共类型，extension 依赖它方向正确（extension → core）。

### 3.4 装载时序

```
进程启动（cli 入口）
  1. 载配置 → AppConfig（含 extensions: dict[str, dict]）
  2. 建 ToolBus，install_builtin_tools(bus)                    ← T1
  3. ExtensionLoader.load_all(bus, app_config)
       对每个发现到的 extension（内置优先，用户级其后）：
         import 模块 → 取 setup → await setup(host)
         失败：记错误、跳过，其余继续
     → ExtensionRegistry{tools 已进 bus, hook_factories, recall_factories, sync_factories}
  4. Boot.from_config(app_config, tool_bus=bus, extensions=registry)

Boot.build_run(agent_id)
     scope = AgentScope(agent_id, app_config)
     recall_sources = [r for f in registry.recall_factories if (r := f(scope))]
     handlers      = [h for f in registry.hook_factories   if (h := f(scope))]
     Run.open(..., tool_bus=bus, recall_sources=recall_sources,
              lifecycle_hooks=LifecycleHooks(handlers=handlers))   ← hook 首次接进生产路径

Boot.build_session_service(agent_id)
     syncs = [s for f in registry.sync_factories if (s := f(scope))]
     SessionService(repository, CompositeSessionSync(syncs))

/reload
     bus.unregister_origin(ToolSource.EXTENSION, <每个 ext 名>)
     await teardown 各 extension
     重跑步骤 1 与 3（同一个 bus 实例，内置工具不受影响）
     Boot.from_config(new_config, tool_bus=bus, extensions=new_registry)
```

### 3.5 合并语义

| 扩展点 | 多个 extension 都贡献时 |
| --- | --- |
| 工具 | 各自独立命名空间 `ext__<extension>__<tool>`（T1），不会撞名 |
| hook handler | 按装载序进 `LifecycleHooks(handlers=[...])`，沿用既有合并规则（deny > ask > allow、`model_context` 最后一个非 None 覆盖、feedback 拼接）。单个 handler 抛异常已被 `hooks/lifecycle.py:_call` best-effort 吞掉 |
| recall source | 按装载序逐个 await，返回的消息依次拼进 history（`prepare.resolve_recalls` 现有行为） |
| session sync | `CompositeSessionSync` 逐个调用；**单个 sync 抛异常不影响其余，也不影响会话主流程**（记错误后继续），与 hook 的 best-effort 一致 |

装载序：内置 extension 先、用户级后；同级按目录名字典序。确定性优先于灵活性 —— E1 不做显式 priority 字段。

---

## 4. 配置

`config.yaml`：

```yaml
extensions:
  openviking:
    enabled: true
    base_url: ${OPENVIKING_BASE_URL}
    account_id: ${OPENVIKING_ACCOUNT_ID}
    user_id: ${OPENVIKING_USER_ID}
    user_key: ${OPENVIKING_USER_KEY}
    commit_after_minutes: 30
    commit_after_turns: 8
    tool_output_max_chars: 4000
    session_recall:
      enabled: false
      max_chars: 6000
      limit: 5
    agents:
      Pickle:
        remote_agent_id: ${OPENVIKING_AGENT_ID}
```

`AppConfig` 侧：

```python
    extensions: dict[str, dict[str, Any]] = Field(default_factory=dict)
```

core 不校验 extension 的配置内容，只做 `expand_env_vars`（沿用现有机制）。extension 自己解析：

```python
    def config(self, model: type[T]) -> T | None:
        """按 extension 名取自己那段配置，用给定 pydantic 模型解析。

        该段不存在 → None；存在但校验失败 → ExtensionConfigError（装载失败，隔离）。
        """
```

**旧的顶层 `openviking:` 段废弃**（决策已定）。`AppConfig.openviking` 字段与那行 import 一并删除。`config/migrate.py` 增一条：迁移时把旧顶层 `openviking` 段折算进 `extensions.openviking`，并在日志里说明。当前 `config.yaml` 的 `openviking.enabled` 是 `false`，破坏面很小。

### 密钥从 auth.json 来

现状 `config/loader.py:88-98` 对 openviking 有一段专门逻辑：settings 里的 `openviking`（策略）与 `auth.json` 里的 `openviking`（`user_key` 等密钥）深合并，auth 优先。`migrate.py` 也按「策略进 settings、密钥进 auth」拆分。

extension 化后这段要通用化，而不是删掉 —— 否则 extension 的密钥只能写进 settings，与既有的密钥分离约定相悖：

```python
        # settings 的 extensions.<name> 与 auth.json 的 extensions.<name> 深合并，auth 优先
        extensions: dict[str, Any] = dict(merged.get("extensions") or {})
        auth_extensions = global_auth.get("extensions") or {}
        if isinstance(auth_extensions, dict):
            for name, auth_section in auth_extensions.items():
                if isinstance(auth_section, dict):
                    extensions[name] = deep_merge(extensions.get(name) or {}, auth_section)
        merged["extensions"] = extensions
```

`auth.json` 的形态：

```json
{
  "providers": { "...": {} },
  "extensions": {
    "openviking": { "user_key": "...", "account_id": "...", "user_id": "..." }
  }
}
```

`migrate.py` 的折算相应分两路：旧 `auth.json` 顶层 `openviking` 段 → `auth.json` 的 `extensions.openviking`；旧 settings 顶层 `openviking` 段 → settings 的 `extensions.openviking`。

`auth_providers` 那条既有通路（`AppConfig.auth_providers`，供 `resolve_model_config` 回填模型密钥）不动 —— 那是 provider 的密钥，与 extension 无关。

### 启用与禁用

`enabled` 由 extension 自己的配置模型定义（openviking 已有该字段），core 不强制。理由：core 不认识 extension 的配置模型，无法在解析前判断 `enabled`；而 extension 在 `setup` 里 `if not config.enabled: return` 一行就够，比 core 多加一层 `extensions.<name>.enabled` 约定更简单。

**代价**：`config.yaml` 里没有 `extensions.<name>` 段的 extension 会以默认配置装载。对内置 extension 这是期望行为（openviking 的 `enabled` 默认 `False`，等于不配就不启用）。

---

## 5. 发现与装载

| 来源 | 位置 | 说明 |
| --- | --- | --- |
| 内置 | `src/pickel/extensions/<name>/` | 随包分发，`importlib.import_module(f"pickel.extensions.{name}")` |
| 用户级 | `~/.pickel/extensions/<name>/`（含 `__init__.py`）或 `~/.pickel/extensions/<name>.py` | `importlib.util.spec_from_file_location`，模块名前缀 `pickel_ext_<name>` 避免与已导入模块撞 |

跳过规则：名字以 `_` 或 `.` 开头的条目、非目录且非 `.py` 的文件。

`PICKEL_HOME` 环境变量已被 `paths.home_dir()` 支持，测试用它指向临时目录即可，不需要额外的注入口子。

### 失败隔离

| 失败 | 行为 |
| --- | --- |
| import 抛异常 | 记 `ExtensionLoadError`（含 traceback），跳过该 extension，其余继续装载 |
| 模块没有 `setup` | 同上，错误信息明确说缺 `setup(host)` |
| `setup` 抛异常 | 同上；**已经注册进 bus 的工具要回滚**（`bus.unregister_origin(EXTENSION, name)`），避免半装状态 |
| 配置校验失败 | `ExtensionConfigError`，按装载失败处理 |

装载错误汇总后由 CLI 渲染成警告，**不阻止启动** —— 一个坏扩展不该让整个 CLI 起不来。`failIfUnavailable` 那类严格模式归 E2。

---

## 6. openviking 改造

| 步骤 | 动作 |
| --- | --- |
| 1 | `SessionSync` Protocol + `NoopSessionSync` 从 `integrations/openviking/session_sync.py` 移到 `conversations/session_sync.py`；`conversations/service.py` 改 import |
| 2 | `git mv src/pickel/integrations/openviking src/pickel/extensions/openviking`（13 文件 1061 行，内部 import 路径批量改） |
| 3 | 新增 `extensions/openviking/__init__.py` 的 `setup(host)`，把 `boot._build_recall_sources` / `_build_session_sync` / `_build_session_recall_provider` / `_resolve_openviking_remote_agent_id` 四个方法的逻辑搬进去，改写为两个工厂函数 |
| 4 | `app/boot.py` 删掉这四个方法与全部 openviking import（四个方法约 97 行 + 14 行 import，`boot.py` 从 220 行降到约 110 行） |
| 5 | `config/app_config.py` 删 `openviking` 字段与 `from pickel.integrations.openviking.config import OpenVikingConfig` |
| 6 | `OpenVikingConfig` 增 `agents: dict[str, OpenVikingAgentConfig]` 的兼容读取 —— 现状 `remote_agent_id` 既可能在 `openviking.agents.<id>`，也可能在 `agents.<id>.remote_agent_id`（`AgentConfig.remote_agent_id`）。后者是 core 的 agent 配置字段，**保留不动**，extension 通过 `AgentScope.app_config` 读它 |
| 7 | `config/migrate.py` 增旧段折算 |

改造后 `boot.py` 里与 openviking 相关的代码：**零行**。这是 E1 是否成功的判据。

---

## 7. 错误处理

| 情形 | 行为 |
| --- | --- |
| extension 装载失败 | 隔离、记错、继续；已注册工具回滚 |
| 工厂求值抛异常 | 记错，该贡献视为 `None`（等于该 agent 不启用它），不影响 Run 构造 |
| hook handler 抛异常 | 沿用 `lifecycle.py:_call` 的 best-effort 吞掉 |
| recall source 抛异常 | **现状 `resolve_recalls` 无保护，会冒泡打断 turn。** E1 补上：逐源 try/except，失败源记错后跳过 |
| session sync 抛异常 | `CompositeSessionSync` 记错后继续下一个 |
| 两个 extension 同名（内置与用户级重名） | 用户级覆盖内置，记一条 warning。理由：允许用户就地替换内置实现 |
| extension 注册的工具名与内置工具撞 | 由 T1 的 `ext__` 前缀保证不会撞 |

---

## 8. 测试计划

| 层 | 用例 |
| --- | --- |
| `ExtensionHost` | 四个 register 方法各自把贡献放进 registry；`config(Model)` 解析成功 / 段缺失返回 None / 校验失败抛 `ExtensionConfigError` |
| `ExtensionLoader` | 发现内置与用户级；跳过 `_`/`.` 前缀；用户级同名覆盖内置并 warning；import 失败隔离且其余继续；缺 `setup` 报明确错误；`setup` 抛异常时已注册工具被回滚；`async def setup` 被 await |
| 工厂求值 | 返回 `None` 的工厂被过滤；工厂抛异常时该贡献被跳过且不影响其余；`AgentScope` 带对的 agent_id |
| `CompositeSessionSync` | 逐个调用；单个抛异常不影响其余；空列表等价 Noop |
| `Boot` 装配 | `build_run` 的 `recall_sources` 与 `lifecycle_hooks` 来自 registry；`build_session_service` 用 Composite；不同 agent_id 得到不同贡献集 |
| recall 异常隔离 | 一个 recall source 抛异常，turn 不中断，其余 recall 仍生效（**新增保护，现状会冒泡**） |
| openviking 回归 | 现有 `tests/integrations/` 用例迁移后全绿；`enabled: false` 时零注册；`session_recall.enabled` 分支行为不变 |
| 解耦验收 | `grep -r "openviking" src/pickel/app/ src/pickel/config/` 无命中（migrate.py 的迁移逻辑除外） |
| `/reload` | 重载后 extension 工具被卸载再注册；内置工具不受影响；`teardown` 被调用 |

---

## 9. 与 T1 / T2 / S2 的关系

| 项 | 关系 |
| --- | --- |
| T1 | **前置**。`register_tool` 直接用 `ToolBus`；`ext__<name>__` 前缀与 `unregister_origin` 是 E1 装卸的基础 |
| T2 MCP 客户端 | **E1 的第一个真实用户**：实现为内置 extension，用 `register_tool` + `teardown` 管子进程。若 E1 的 API 喂不了它，说明 API 不够用 —— 这是设计的检验点 |
| S2 沙箱 | 两处交集：① extension 是**跑在本进程内的第三方代码**，沙箱管不到它，信任模型只能靠「发现目录分级 + E2 的信任门」；② 参考 `pi-sandbox`，沙箱**策略层**可以是 extension（挂 `PreToolUse` 决定放不放行），OS 原语留在 core |
| V1 版本管理 | extension 的版本、依赖、分发复用 V1 的统一模型；E1 只认目录，不认版本 |

---

## 10. 遗留取舍

1. **extension 跑在 runtime 进程内，无隔离。** 用户级目录里的代码拿到与 core 同等权限。E1 靠「只发现内置 + 用户自己的目录」把风险限定在用户自己放进去的代码；项目目录来的代码（最危险的一类）与信任门一起推到 E2。
2. **`enabled` 由 extension 自己判断**，core 无法在解析配置前跳过一个 extension。代价见 §4。
3. **装载序固定为「内置 → 用户级，同级字典序」**，无显式 priority。多个 extension 争同一位点（例如两个都想改 `model_context`）时，靠既有合并规则（最后一个覆盖）裁决。若将来出现真实冲突，再引入 priority。
4. **`setup` 只在启动与 `/reload` 时跑一次**，没有 Pi 的「运行时动态注册」（`session_start` 内 `registerTool` 立即生效）。E1 的注册表在 turn 边界之外变更，与 T1 的快照语义天然兼容；动态注册留给 E2。
5. **`AgentScope` 只带 `agent_id` + `app_config`。** 工厂拿不到 Run / Session（它们此时还不存在）。需要会话级状态的 extension 只能在 hook handler 的事件里拿 —— 这与 Pi 的 `ctx` 分层一致。
