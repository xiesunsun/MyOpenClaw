# Slash 命令与 Extension 演进需求整理

**日期**：2026-07-28
**分支语境**：`feature/context-command-align` 及后续
**目的**：把「slash 可选择 + 子补全」「extension 接口」「对标 Pi extension」三块需求收成一份，便于拍板与排期。
**不在本文**：具体代码实现步骤（可另开实施计划）。

**关联**

| 文档 | 关系 |
|------|------|
| `2026-07-26-extension-host-design.md` | E1 已落地的 ExtensionHost |
| `2026-07-26-tools-sandbox-research.md` | 调研：extension ≠ 工具打包 |
| `2026-07-27-cli-render-blocks-enhancement.md` | CLI 展示（流式/工具/context）已落地说明 |
| Pi 文档 [Extensions](https://pi.dev/docs/latest/extensions) | 标杆能力面 |

---

## 一、问题与目标

### 1.1 现状问题

| 区域 | 现状 | 痛点 |
|------|------|------|
| Slash 输入 | 必须手敲 `/help` `/model` … | 不能方向键选择 |
| 命令元数据 | header / help / if-elif 分派三处手写 | 易漂移 |
| 子参数 | `/model` `/agent` 等无补全 | 记不全 id |
| Extension | 有工具/hook/recall/sync | **无** `register_command` |
| 动态 MCP/插件 | ToolBus 可热更工具 | slash 列表写死，跟不上插件命令 |

### 1.2 目标（产品）

1. 输入 `/` 后可 **列表 + 方向键/Tab 选择** 命令。
2. **子补全一并做**：`/model` `/agent` `/thinking` `/skills`，并预留 **`/tools`**。
3. 补全数据 **可热更新**（reload、MCP 增减、pending skill、插件装卸）。
4. 架构上为 **插件注册 slash** 留好口，并对齐 Pi `registerCommand` 思路。
5. **输入层不依赖 Boot/Run**；执行仍在 ChatLoop / extension handler。

---

## 二、Slash 命令改动需求

### 2.1 交互

```text
You > /
  /help      Show help          ← ↑↓ 选择，Enter/Tab 确认
  /model     List or set model
  /tools     List tools (MCP/builtin/ext)
  …

You > /model anth
  anthropic/claude-jupiter-v1-p
  …

You > /skills approve
  <pending_id> …
```

- 仅当输入以 `/` 开头时出命令补全；普通对话不弹菜单。
- 确认后写入输入行（或直接执行，实现时二选一，建议 **填入后 Enter 再执行**，与现循环一致）。

### 2.2 命令清单（一级）

| 命令 | 子补全 | 补全源（概念） |
|------|--------|----------------|
| `/help` | 无 | — |
| `/model` | 有 | `app_config.providers` → `provider/model` |
| `/thinking` | 有 | 固定枚举（如 low/medium/high/xhigh/off；执行仍可接受任意字符串，与现逻辑兼容） |
| `/agent` | 有 | `app_config.agents` 键 |
| `/new` | 无 | — |
| `/reload` | 无 | — |
| `/context` | 无 | — |
| `/session` | 无 | — |
| `/skills` | 有 | 子命令 `pending\|diff\|approve\|reject`；后三者再补 `pending_id` |
| `/clear` | 无 | — |
| `/exit` | 无 | — |
| **`/tools`（新增）** | 有 | `tool_bus.snapshot(activation).names`（builtin + `mcp__*` + `ext__*`） |

`/tools`：列工具、支持名字过滤补全；**不**与 `/model` 混在同一层。

### 2.3 技术选型

| 项 | 选择 |
|----|------|
| 库 | **已有 prompt_toolkit**（Completer + complete_while_typing） |
| 不上 | 默认全屏 questionary；不上 Textual 命令板（除非另立 TUI 项目） |

### 2.4 架构（解耦）

```text
ChatLoop（装配 + 执行）
  │ 实现 CompletionSources（现取 config/run/bus/store）
  │ 维护 name → handler
  ▼
cli/slash/
  registry.py     命令元数据（name/summary/arg_kind）
  sources.py      Protocol：list_models/agents/thinking/skills/tools…
  completer.py    prompt_toolkit Completer
  parse.py        解析 "/cmd arg"
  ▼
prompt_input.py   PromptSession(completer=…)
```

**依赖方向（强制）**

```text
chat → slash → prompt_toolkit
chat → boot/config/run/bus
slash ↛ boot / run / config
```

**CompletionSources（示意）**

| 方法 | 现取真源 |
|------|----------|
| `list_models()` | `_list_available_models()` / providers |
| `list_agents()` | `app_config.agents` |
| `list_thinking_levels()` | 静态枚举（或日后配置） |
| `list_skills_actions()` | pending/diff/approve/reject |
| `list_skills_pending_ids()` | `skill_store.list_pending()` |
| `list_tool_names()` | `tool_bus.snapshot(activation).names` |

规则：**每次补全调用都现取**，禁止 Completer 构造时拷贝死列表 → 才能跟 `/reload`、MCP、pending 变化。

### 2.5 与执行路径关系

- Completer **只改输入字符串**，不执行业务。
- 回车后仍：`startswith("/")` → `_handle_command`（可改为 registry/handlers 表）。
- header、`/help` 文案 **从 registry 生成**，消灭三处手写。

### 2.6 分期（Slash）

| 期 | 内容 |
|----|------|
| **S1** | registry + Completer 一级菜单 + Chat 装配 |
| **S2** | 子补全 model/agent/thinking/skills + **`/tools`** |
| **S3** | handlers 字典替换 if/elif；help/header 全走 registry |

S1+S2 可同 PR；S3 可同批或紧随。

---

## 三、涉及 Extension 的设计改动

### 3.1 现状（E1 已落地）

**宿主**：`pickel.extensions_host`
**内置样板**：`extensions/openviking`、`extensions/mcp`

| Host API | 用途 |
|----------|------|
| `config(Model)` | 解析 `extensions.<name>` |
| `register_tool` | 进程级工具 `ext__<name>__*` |
| `register_mcp_tool` / `unregister_mcp_origin` | MCP 工具 `mcp__<server>__*` |
| `add_hook_handler` | per-agent hook 工厂 |
| `add_recall_source` | per-agent 召回 |
| `add_session_sync` | per-agent 会话同步 |

**明确没有（E1 不做）**：`register_command`、项目级信任门、`add_skill_path`、UI API。

### 3.2 为 Slash / 动态能力需要的改动

| 改动 | 动机 | 难度 |
|------|------|------|
| **`/tools` + `list_tool_names`** | MCP/插件工具可发现、可补全 | **低**（读现有 ToolBus） |
| **SlashRegistry 可变** | 插件可增删 `/foo` | **中** |
| **`host.register_slash_command(spec, handler?)`** | 对标 Pi `registerCommand` | **中** |
| **teardown / reload 按 origin 卸命令** | 与 `unregister_origin` 工具对称 | **中** |
| **分派：builtin handlers + extension dispatch** | 补全能选到也能执行 | **中** |
| （可选）`list_tool_names` 过滤 activation | 与 prepare 一致 | 低 |

### 3.3 推荐演进顺序（Extension 相关）

```text
① Slash 静态 11 命令 + Sources 现取
② /tools + list_tool_names（验证「现取 Bus」）
③ Registry 可变 + register_slash + reload 挂钩
④ （更后）项目级 extensions + 信任门、add_skill_path 等 E2 原清单
```

①② **不依赖** ③；③ 不推翻 Completer 模型，只让 `registry.list()` 从「常量」变成「可注册集合」。

### 3.4 动态更新如何保证

| 变化 | 机制 |
|------|------|
| `/reload` | 换 app_config/run；Sources 现读新引用 |
| MCP 增减 | Bus 变更；`list_tool_names` 下次 Tab 新列表 |
| skill pending | `list_pending()` 现读 |
| 插件工具 | `register_tool` / teardown origin |
| 插件命令（③ 后） | `register_slash` / unregister_origin |

**禁止**：Completer 内缓存启动时的命令/模型/工具列表。

---

## 四、对标 Pi Extension：需求与差距

依据：[Pi Extensions 文档](https://pi.dev/docs/latest/extensions)。

### 4.1 能力对照

| 能力 | Pi | Pickel 现状 | 本需求下的动作 |
|------|-----|-------------|----------------|
| 注册 LLM 工具 | `registerTool` | `register_tool` / MCP | 保持；`/tools` 消费 Bus |
| 生命周期/事件 | 极丰富 `pi.on(...)` | LifecycleHooks 子集 | 不在本 slash 需求扩大事件表 |
| **用户命令** | **`registerCommand`** | **无** | **要做：register_slash** |
| 补全/发现命令 | 与命令一体 | 手敲 | **SlashCompleter + Registry** |
| 快捷键 / Flag | 有 | 无 | 非本需求 |
| UI（confirm/select） | `ctx.ui` | 无统一 API | 非本需求；命令 handler 可先 `console` |
| Session 旁路 entry | `appendEntry` | 各 extension 自建 | 非本需求 |
| Skill 路径贡献 | `resources_discover` | 无 | E2 原清单，非本需求必做 |
| Provider 注册 | `registerProvider` | 无 | 非本需求 |
| 热重载 | `/reload` | `/reload` 卸装 extension | 命令注册需接入同一路径 |
| 发现目录 | 全局+项目+信任 | 内置+用户 | 项目级仍 E2 |

### 4.2 已对齐的设计哲学

- Extension 改 **harness 行为**，不是 skill 文档 alone。
- 工具进程级总线 vs 会话/ agent 贡献分离。
- 配置段自解析；失败隔离。
- reload 可重装。

### 4.3 本需求要补的「Pi 级」缺口（最小集）

| 缺口 | 对应 Pi | 我们落地物 |
|------|---------|------------|
| 可发现、可点选的命令 | 命令 + TUI/补全 | prompt_toolkit Completer + Registry |
| 插件自定义 `/cmd` | `registerCommand` | `host.register_slash_command` + 可变 Registry |
| 工具可枚举 | `getAllTools` 类能力 | `/tools` + `snapshot.names` |

**不在本需求强行对齐**：全量事件表、`ctx.ui.custom`、npm 包分发。

### 4.4 开发者规格（现状 vs 目标）

**现在写 extension 只需：**

```python
def setup(host: ExtensionHost) -> None:
    cfg = host.config(MyConfig)
    if not cfg or not cfg.enabled:
        return
    host.register_tool(...)
    host.add_hook_handler(...)
    # add_recall_source / add_session_sync
```

**目标增加（slash 插件）：**

```python
def setup(host: ExtensionHost) -> None:
    host.register_slash_command(
        name="foo",           # → /foo；冲突策略另定
        summary="...",
        # handler: 同步/异步 (arg, ctx) -> None
        # 或仅注册元数据，执行走 extension 消息端口
    )
```

Handler 上下文（建议最小集，对标 Pi 的缩小版 `ExtensionCommandContext`）：

| 字段 | 用途 |
|------|------|
| `console` / 打印回调 | 输出 |
| `session` 只读可选 | 展示 |
| **不**直接给 Boot | 保持边界 |

细则实施计划再定；需求层先锁定 **「能注册 + reload 能卸 + 补全能见」**。

---

## 五、架构好不好改（结论复述）

| 项 | 判断 |
|----|------|
| 加 `/tools` | **好改**：Sources 一方法 + 一命令 + handler |
| 插件动态 `/foo` | **好改、多半层**：Registry 可变 + Host API + 分派；模式对齐 ToolBus origin |
| 会否推翻现有 E1 | **否**；在 Host/Bus/reload 上延伸 |

---

## 六、验收标准（需求级）

### Slash

- [ ] `/` 出命令列表，方向键可移动，可过滤
- [ ] `/model` `/agent` `/thinking` `/skills` 子补全可用
- [ ] `/tools` 列出并过滤当前激活工具名（含 mcp__/ext__）
- [ ] `/reload` 后 model/agent/工具/pending 补全为新数据
- [ ] 非 `/` 输入无命令菜单打扰

### Extension（命令插件，若做 S3/扩展期）

- [ ] extension `setup` 可注册 slash；补全可见
- [ ] teardown/reload 后命令消失
- [ ] 与 builtin 撞名有明确策略（拒绝或后缀）

### 回归

- [ ] 现有 extension（openviking、mcp）行为不变
- [ ] 无 completer 的测试 input_reader 仍可用

---

## 七、非目标（本整理范围外）

- 终态 Markdown 重渲、工具 ANSI 擦屏（已否决）
- TUI 全屏命令板
- Pi 级 `ctx.ui` / 快捷键 / Flag
- 自动 compact 80% 假提示
- extension 进程沙箱

---

## 八、建议排期（一页）

| 序号 | 交付 | 依赖 |
|------|------|------|
| 1 | `cli/slash`：registry + sources + completer + 装配 | prompt_toolkit 已有 |
| 2 | 子补全 + **`/tools`** | ToolBus snapshot |
| 3 | help/header/handlers 统一 registry | 1 |
| 4 | `register_slash` + reload 挂钩 + 文档《Extension 开发者指南》增补 | 1–3、E1 Host |
| 5 | （可选）项目级 extensions + 信任门 | E2 原清单 |

---

## 九、一句话

**Slash：用 prompt_toolkit + 现取 Sources 做可选命令与子补全，并加 `/tools` 消费 ToolBus。**
**Extension：在 E1 Host 上补「可变 SlashRegistry + register_slash」，对标 Pi 的 registerCommand，不动工具总线主模型。**
**动态：一切补全现取真源，与 `/reload`、MCP、插件装卸自然一致。**
