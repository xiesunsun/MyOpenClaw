# T2 MCP 客户端设计稿

日期：2026-07-26。前置：T1 工具总线（已完成）、E1 extension 宿主（已完成）。

目标：把 stdio MCP server 的工具接入 pickel 工具总线，实现为一个内置 extension——同时检验 E1 extension API 是否够用（调研文档 4b 的「试金石」）。

## 1. 范围

**做**：stdio 传输；`.mcp.json` 配置；启动全连 + 失败隔离；工具发现与 `mcp__<server>__<tool>` 命名；调用时重连；白名单 `mcp__<server>__*` 通配。

**不做（YAGNI，后续按需）**：HTTP/SSE 传输；resources / prompts / sampling / roots / elicitation；后台健康检查；MCP 工具调用审批流（信任由用户写 `.mcp.json` 这个动作表达，进程级防线在 S2 sandbox）。

## 2. 决策记录

| 问题 | 决策 | 理由 |
| --- | --- | --- |
| 传输 | 仅 stdio | 覆盖绝大多数 server，远程传输后续按需加 |
| 配置 | 独立 `.mcp.json` | Claude Code 同款约定，生态互通，已有文件可直接复用 |
| 连接时机 | 启动时全连 | 工具列表从第一个 turn 起完整；失败隔离不阻断启动 |
| 断线处理 | 调用时重连一次 | 无常驻线程；仍失败 → is_error + `unregister_origin` |
| 白名单 | 支持 `mcp__<server>__*` 通配 | server 工具集升级不用追改 agent.yaml |

## 3. 架构

内置 extension `mcp`（`src/pickel/extensions/mcp/`），走 E1 宿主装载。模块分工：

```
src/pickel/extensions/mcp/
  __init__.py     # setup(host)：读配置 → 逐 server 连接 → 注册代理工具
  config.py       # .mcp.json 发现、解析、合并（McpServerSpec）
  connection.py   # McpConnection：子进程 + ClientSession 生命周期、重连
  proxy.py        # McpProxyTool(BaseTool)：schema 与结果转换
```

### 3.1 E1 API 扩展（试金石发现）

`ExtensionHost.register_tool` 固定 `source=EXTENSION, origin=<extension 名>`（`ext__` 前缀），MCP 工具需要 `source=MCP, origin=<server 名>`（`mcp__` 前缀）——执行位置与信任级别不同，T1 设计里名字必须能区分。宿主增两个窄接口，不开放任意 source/origin：

```python
class ExtensionHost:
    def register_mcp_tool(self, tool: BaseTool, *, server: str) -> str:
        """注册 MCP 代理工具。最终名为 mcp__<server>__<tool>。"""
        return self._tool_bus.register(tool, source=ToolSource.MCP, origin=server)

    def unregister_mcp_origin(self, server: str) -> list[str]:
        """卸掉某个 MCP server 的全部工具（断连/重连失败路径）。"""
        return self._tool_bus.unregister_origin(ToolSource.MCP, server)
```

试金石结论（实施后共三条发现）：

1. E1 API 缺「以非本 extension 身份注册工具」——补 `register_mcp_tool`/`unregister_mcp_origin`。
2. `ExtensionHost` 原本不暴露 `app_config`——mcp 需要项目根定位 `.mcp.json`，已增 `host.app_config`。
3. **extension 装载必须发生在使用方的事件循环里**——MCP 连接由背景任务持有，`_boot` 的同步 `load_extensions`（内部 `asyncio.run`）建的连接会随临时循环死掉，chat 首调只能靠重连兜住。已为 chat 增 `_boot_async` 路径（其余 CLI 命令仍走同步装载）。

其余（async setup、teardown、config 解析、装载失败隔离）全部够用。

### 3.2 配置：`.mcp.json`

格式与 Claude Code 兼容：

```json
{
  "mcpServers": {
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}"}
    }
  }
}
```

- 查找与合并：`~/.pickel/.mcp.json`（全局）+ workspace 根 `.mcp.json`（项目），同名 server 项目覆盖全局。两个文件都可缺省。
- `env` 值支持 `${VAR}` 展开（从进程环境取，缺失保留原文并记 warning）；子进程环境 = 继承进程环境 + env 覆盖。
- server 名含 `__` → 该 server 记 warning 跳过（bus 的 origin 校验兜不住时提前挡）。
- extension 的启停沿用 settings.json 的 `extensions.mcp.enabled`（默认 true，与其他 extension 一致）；server 列表本身只认 `.mcp.json`。

### 3.3 连接管理：McpConnection

mcp SDK（`mcp>=1.26.0`，依赖已声明）的 `stdio_client` / `ClientSession` 都是 async context manager，且内部 anyio task group 要求**进入与退出发生在同一 asyncio 任务**。因此每个连接由一个专属背景任务持有：

```
McpConnection(spec)
  ├─ _runner_task：async with stdio_client(...) as (r, w):
  │                  async with ClientSession(r, w) as session:
  │                    initialize → list_tools → set ready
  │                    await _shutdown_event.wait()
  ├─ session：ready 后对外可用；call_tool 可从任意任务调用（SDK 内部按请求 id 复用）
  ├─ tools：发现结果（list[mcp.types.Tool]）
  └─ close()：set _shutdown_event → await _runner_task
```

- `setup(host)` 为 async（loader 已支持）：逐 server `McpConnection.open()`，成功后为每个发现的工具 `host.register_mcp_tool(McpProxyTool(connection, tool), server=name)`。
- 单 server 连接/initialize/发现失败：记 warning，跳过该 server，其余照常——与 extension 装载失败隔离同语义。启动不因任何 server 阻断。
- 连接超时：initialize + list_tools 整体 10s，超时按失败处理。
- 单次工具调用超时 60s（asyncio.wait_for），超时转 is_error，不触发重连（连接还活着）。
- 死亡探测（实施发现）：子进程死后持有栈的背景任务仍阻塞在 shutdown 事件上，`runner.done()` 探测不到；连接丢失由 call_tool 的异常类型判定（anyio ClosedResource/BrokenResource、McpError "Connection closed"）并置 `_dead`。
- teardown：全部 `connection.close()`（/reload 走这里，重装后重读配置重连）。

### 3.4 工具代理：McpProxyTool

- `ToolSpec.name` = MCP 工具名；`description` 直传；`input_schema` = MCP `inputSchema` 直传（都是 JSON Schema，无需转换）。
- `execute`：`session.call_tool(name, arguments)` →
  - content 里的 `TextContent` 按序拼接为 `content` 字符串；
  - 非文本块（image / embedded resource）首版转占位行 `[unsupported content: <type>]`，记 metadata；
  - MCP 的 `isError` 直传 `is_error`；
  - metadata 带 `server`、`mcp_tool` 便于日志归属。
- 调用异常（连接死、管道断）进入重连路径（3.5）。

### 3.5 调用时重连

```
call_tool 抛连接类异常 / 连接已死
  → 重连一次：close 旧连接 → 新 McpConnection.open()
      → 成功：先 unregister_mcp_origin(server) 再全量重注册发现的工具
              （先卸后注保证 server 升级后消失的工具被剔除）
              → 重试调用一次，结果照常返回
      → 失败：unregister_mcp_origin(server) 卸掉该 server 全部工具
              → 返回 is_error（"MCP server '<name>' is unavailable..."）
```

- 先卸后注不保留 bus 覆盖语义里的 enabled 状态——可接受，重连是异常路径。
- per-connection asyncio.Lock 防并发重连：同 server 多个工具同时失败只重连一次，其余等待复用。
- 重试只做一次：重试中再失败直接 is_error，不递归。

### 3.6 白名单通配

`ToolActivation.allowed` 支持含 `*` 的模式（`fnmatch.fnmatchcase`），影响 `ToolBus` 两处：

- `snapshot()`：`entry.name in allowed` 改为「精确命中或匹配任一通配模式」；`agent_disabled` 照旧精确名（tool_set_active 操作的是快照里的具体名字）。
- `missing_names()`：精确名照旧「bus 里没有即 missing」；通配模式改为「bus 里没有任何名字匹配它才 missing」。

agent.yaml 写法：`- mcp__github__*`（整 server 放行）或精确 `- mcp__github__create_issue`。内置工具名不含 `__`，通配语义对现有配置零影响。

## 4. 工具面变化汇总

| 位置 | 变化 |
| --- | --- |
| `ExtensionHost` | 增 `register_mcp_tool` / `unregister_mcp_origin` |
| `ToolBus` | `snapshot` / `missing_names` 支持通配模式 |
| `src/pickel/extensions/mcp/` | 新增（config / connection / proxy / setup） |
| agent.yaml | 按需增 `mcp__<server>__*`（不随本子项目改动默认配置） |

## 5. 错误处理汇总

| 场景 | 行为 |
| --- | --- |
| `.mcp.json` 不存在 | extension 静默不注册任何工具 |
| `.mcp.json` 解析失败 | 该文件记 warning 整体跳过（另一层级文件照常生效） |
| server 名含 `__` | 该 server 记 warning 跳过 |
| 连接/initialize/发现失败 | 该 server 记 warning 跳过，不阻断启动 |
| 调用时连接死 | 重连一次重试；仍失败 → is_error + 卸载该 server 工具 |
| `env` 的 `${VAR}` 缺失 | 保留原文 + warning |

## 6. 测试计划

夹具：用 mcp SDK 自带的 FastMCP 写最小 stdio server 脚本（`tests/extensions/mcp/fixture_server.py`：echo 工具 + error 工具 + 慢工具），测试以 `sys.executable` spawn。

| 层 | 覆盖 |
| --- | --- |
| config 单测 | 发现/合并/覆盖、`${VAR}` 展开、坏 JSON 隔离、`__` server 名跳过 |
| 通配单测 | snapshot 通配命中、missing_names 两种语义、现有精确名回归 |
| proxy 单测 | schema 直传、TextContent 拼接、isError 直传、非文本占位 |
| 集成 | 真连接：发现→注册→调用→teardown；杀掉子进程后调用→自动重连成功；server 命令不存在→失败隔离启动不断 |

## 7. 遗留取舍

1. **调用审批流不做**——`.mcp.json` 是用户亲手写的信任声明；进程级隔离归 S2。
2. **工具列表变更通知（`listChanged`）不订阅**——重连时全量重发现已覆盖主要场景；订阅推送等有真实需求再加。
3. **每 server 一个背景任务而非共享事件循环结构**——anyio cancel scope 的同任务约束决定的，也天然隔离单 server 的崩溃。
4. **重连先卸后注丢 enabled 状态**——异常路径可接受，换实现简单。
