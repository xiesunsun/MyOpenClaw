# 模型请求组装（prepare / Request）升级设计

**状态**：设计稿（修订，待审阅）  
**分支**：`feature/context-request-prepare-design`  
**范围**：Request 组装、热重载与 slash 边界、与现代码映射  
**不在范围**：记忆产品定稿、PlanExecute/Reflection、配置分层（已见 config-system 文）

**相关现状代码**：`agents/skills.py`、`agents/agent.py`、`context/assembler.py`、`context/projection.py`、`runs/run.py`、`runs/strategy/react.py`、`hooks/*`、`cli/chat.py`、`app/boot.py`  
**配置域**：`docs/upgrade/2026-07-25-config-system-design.md`

---

## 1. 目标

| 目标 | 说明 |
|------|------|
| 文案可改 | system 拼装用固定字外置，改字不改 Python |
| 组装唯一路径 | 只经 `prepare` 阶段表；ReAct 不私拼 system |
| 避免上帝类 | 薄编排 + 小模块；无万能 Build/Mount |
| 同进程热更 | skills/templates 等可热更；**一条 `/reload`** 定范围 |
| slash 少而清 | 高频命令少；静默生效的不上 slash |
| 语义清晰 | Agent = 定义；Run = 资源；Session = 日志；Request = 一次调用入参 |

**非目标**

- Agent 当活主体 / process  
- 默认 fork 历史换 agent  
- 热加载 Python 源码  
- 一堆 `/reload-xxx`  

---

## 2. 现状问题（对照代码）

```text
Boot.resolve_agent
  → SkillRegistry.discover 一次  → Agent.skills 冻住
  → compose_system_instruction（SKILL_USAGE_GUIDANCE 硬编码）
ReAct
  → SystemContent.from_text(agent.system_instruction)
  → ContextAssembler.assemble(projection + window + hook_feedback)
  → tools 手写映射
```

| 痛点 | 代码落点 |
|------|----------|
| 文案焊死 | `skills.py` 中 `SKILL_USAGE_GUIDANCE` |
| skills 不热更 | `boot._resolve_agent_skills` 仅启动 |
| 拼接点散 | Agent 属性 / ReAct / Assembler |
| Hook 无改 Request | 仅有执行层 lifecycle |
| slash 少配置类 | 仅 `/help` `/context` `/session` `/clear` `/exit` |

---

## 3. 概念模型

### 3.1 实体

| 名字 | 是什么 | 实体性 |
|------|--------|--------|
| **Agent** | 角色**定义**（`agents/<id>/` 解析结果） | 配置型概念实体，非活主体 |
| **Run** | 定义装载后的资源袋 | 运行时资源，非 OS 进程 |
| **Loop** | 执行回路（`Run.turn` + `ExecutionStrategy`） | 控制流 |
| **Turn / Step** | 一轮用户作业 / 一次 model 调用 | 瞬时作业，非线程 |
| **Request** | 一次 model 入参（现 `ModelContext` 可保留名） | 瞬时值对象 |
| **Session** | 对话 entry 树 | 持久；`agent_id` 创建后不可改 |
| **templates** | system 拼装用固定文案文件 | 磁盘；**不是** pi 的用户斜杠模板 |
| **prompts**（后话） | pi 式用户 `/name` 展开模板 | 若做，单独概念，勿与 templates 混名 |
| **Hooks** | 执行边界 + `before_request` | 运行时 |
| **Recall** | 窄召回源 | 扩展列表，默认可空 |

主流 coding harness（pi / Claude Code）亦多以 Session+Loop 为核心；Agent 有类型时也是 **spec**，与本设计一致。

### 3.2 总览

```text
agents/<id>/ ──► Agent（定义）──► Run + Loop
                      │
Session ──────────────┤
templates ────────────┤
Recall[] ─────────────┤
                      ▼
               prepare（薄编排）
                 resolve_system      # 每 turn 可 re-discover skills
                 resolve_history
                 resolve_recalls
                 resolve_feedback
                 resolve_tools
                 before_request
                      ▼
                  Request ──► Provider
                      ▼
                 Session append
```

### 3.3 命名（Unix / 短名）

| 用 | 不用 |
|----|------|
| prepare、resolve_*、templates、Recall、Run、Session | *Manager、万能 Mount/Build、AgentRuntime、ContextPipeline 上帝类 |
| Settings/Environ（配置域已定） | 运行时再读 config.yaml |

---

## 4. prepare：薄编排 + 阶段表

### 4.1 入口

```text
prepare(run, session, *, hook_feedback=None, step_meta=None) -> Request
```

- 发模型内容的**唯一**路径  
- ReAct **删除** `SystemContent.from_text(run.agent.system_instruction)` 私拼  
- 编排器只 `for stage in stages`，无业务 if  

### 4.2 阶段表

| 序 | 阶段 | 职责 | 现状迁出 |
|----|------|------|----------|
| 1 | `resolve_system` | behavior + **templates** + skills catalog | `compose_*` + 每 turn discover |
| 2 | `resolve_history` | projection + window | `projection` + `window` |
| 3 | `resolve_recalls` | Recall 源注入 | 默认 `[]` |
| 4 | `resolve_feedback` | hook 文本 → 尾部 user | `append_hook_feedback` |
| 5 | `resolve_tools` | tools → ToolDefinition | ReAct 内循环 |
| 6 | `before_request` | Hooks 可改 Request | **新增** |

模块拆分示例：`context/prepare.py`（薄）+ `system.py` / `history.py` / …  

`ContextAssembler`：收进 prepare 内部，**取消**对外组装入口语义。

### 4.3 Skills 与热更（每 turn）

| 项 | 行为 |
|----|------|
| skills **列表**/description | **每 turn 开始**（或 prepare 前）`SkillRegistry.discover(skills_path)`；不依赖 Boot 一次冻结 |
| skills **正文** SKILL.md | 不进进程缓存；模型 read 即最新 |
| `Agent.skills` | 改为路径持有 + 每次解析，或 Run 持 `skills_path` 由 resolve_system 扫 |

---

## 5. templates（system 文案）— 勿称 prompts

**消歧义（对齐 pi）：**

| 名 | 含义 |
|----|------|
| **templates** | 拼 **system** 的固定文案（本设计） |
| **prompts**（pi） | 用户输入 `/name` **展开成 user 消息** 的模板；后话可选，**另一概念** |

```text
包内 pickel/templates/
~/.pickel/templates/
项目/.pickel/templates/
```

合并：默认 < 用户 < 项目。

| 文件例 | 替代 |
|--------|------|
| `skills_guidance.md` | `SKILL_USAGE_GUIDANCE` |
| catalog 行格式 | 小函数或模板 |

P0：默认内容与现网字符串一致。

---

## 6. 热重载矩阵

### 6.1 原则

1. **进行中 step 不热更**（对齐 pi turn snapshot）。  
2. **静默能做的不上 slash。**  
3. **磁盘资源统一 `/reload`。**  
4. **会话试错用 Environ（`/model` `/thinking`）。**  
5. **代码/依赖必须重启进程。**  

### 6.2 静默（无命令）

| 项 | 行为 |
|----|------|
| Skill 正文 | read 文件即新 |
| Skills 列表 | 每 turn re-discover（mtime 缓存可选） |
| templates | prepare 时读盘（或缓存至 `/reload`） |

### 6.3 `/reload` 范围（同进程、同 Session、同 agent_id）

**会做：**

| 类别 | 内容 |
|------|------|
| Skills | 强制 re-discover |
| templates | 重读 system 文案 |
| prompts 目录（若已实现 pi 式） | 重扫斜杠模板列表 |
| Settings 合并结果 | 再 `Config.load` 刷新运行相关项（如 window、max_steps） |
| models.json | 重读能力目录 |
| 当前 agent.yaml + AGENT.md | 重建 Run 资源（tools/workspace/behavior），**Session 不变** |
| auth.json | 重建 Provider；**保留当前 Environ 的 model 选择** |
| 插件/扩展（有后） | 重载扩展运行时（对齐 pi `/reload`） |

**不做：**

| 不做 | 替代 |
|------|------|
| 换 agent_id / 新开或 fork Session | `/agent`、`/new` |
| 改历史 entry | — |
| 打断 in-flight step | — |
| 热加载 Python 源码/依赖 | 重启 `uv run pickel` |
| migrate | CLI `pickel config migrate` |
| 持久 default model 写盘 | CLI `config set-default-model` |

**成功提示示例：**  
`Reloaded skills, templates, settings, models, agent, auth. Session=<id> agent=<id>. Next turn uses new snapshot.`

失败：配置非法则报错并**尽量保持旧快照**。

### 6.4 必须重启进程

- `src/pickel` 源码、依赖版本  
- 内置 tool **实现代码**  
- 进程级基建  

---

## 7. Slash 命令（少而清）

### 7.1 主列表（`/help`）

| 命令 | Session | Run / 其它 |
|------|---------|------------|
| **`/model [provider/model]`** | 同一 Session | 更新 Environ + Provider；无参可下拉/选择器（对齐 pi） |
| **`/thinking <level>`** | 同一 Session | 更新 Environ `provider_options` |
| **`/agent [id]`** | **新开空 Session** | 重建 Run；见 §7.2 |
| **`/new`** | 新开空 Session，**同** agent | Run 可保留 |
| **`/reload`** | 不变 | 见 §6.3 |
| **`/context`** | — | 展示（已有） |
| **`/session`** | — | 展示（已有） |
| **`/clear`** | — | 清屏（已有） |
| **`/help`** | — | 帮助 |
| **`/exit`** | — | 退出 |

### 7.2 `/agent` 交互（名称或列表）

| 用法 | 行为 |
|------|------|
| **`/agent`**（无参数） | 列出当前可用 agent_id（来自 `agents/` 扫描 + Config），**可下拉/选择列表**选中后执行切换 |
| **`/agent <id>`** | 直接切换到该定义 |

**切换语义（定稿）：**

```text
1. 旧 Session 保留在库中（不删）
2. Session.create(agent_id=新, cwd=当前 cwd)  // 空历史
3. 按新定义重建 Run
4. 提示：Switched to <id>, new session <session_id>
```

- **不是**改当前 session 的 `agent_id`  
- **不是**默认拷贝历史  

**分叉拷贝（后话，非主列表）：**  
若需要「带着历史换角色」→ 单独 **`/fork-agent <id>`**（P2+），复制 entry 树到新 session + 新 agent_id；**默认路径不做 fork**。

### 7.3 `/model` 与 `/thinking`

| | 行为 |
|--|------|
| `/model` | 无参：选择器/列表（读最新 models.json）；有参：`provider/model` 或约定格式 |
| `/thinking …` | 如 `low` / `high` / `xhigh` 等，写入 Environ |
| 与 `/reload` | reload 重建 Provider 时 **保留** 当前 Environ 选型，不被 settings 默认覆盖 |

### 7.4 不做 slash（CLI 或静默）

| 项 | 方式 |
|----|------|
| migrate | `pickel config migrate` |
| 持久默认 model | `pickel config set-default-model` |
| 分项 reload | 统一 `/reload` |
| skill 正文 | 静默读文件 |

---

## 8. Hooks

| 层 | 事件 | 职责 |
|----|------|------|
| 执行（已有） | user_prompt_submit、pre/post_tool、post_tool_batch、turn_end | 拦输入/工具、feedback |
| 请求（新增） | **before_request** | 改最终 Request |

长文案 → templates；OV 进模型 → Recall；同步游标 → 旁路 SessionSync。

---

## 9. 与现代码映射（改 / 删 / 增）

### 9.1 删或收缩

| 现有 | 动作 |
|------|------|
| `SKILL_USAGE_GUIDANCE` | 删常量 → templates 文件 |
| ReAct 内 `from_text(agent.system_instruction)` | 删 → `prepare` |
| ReAct 内 tools 映射循环 | 迁 `resolve_tools` |
| `ContextAssembler` 对外入口 | 收进 prepare |
| `Agent.system_instruction` 作唯一真相 | 弱化/委托，避免双源 |
| Boot 仅一次 `discover` 冻死 skills | 改为路径 + 每 turn discover |

### 9.2 增

| 新增 | 说明 |
|------|------|
| `context/prepare.py` + stages | 薄编排 |
| `templates/` 加载 | 合并默认/用户/项目 |
| `before_request` | LifecycleHooks |
| `Recall` Protocol | 默认 [] |
| ChatLoop：`/model` `/thinking` `/agent` `/new` `/reload` | 见 §7 |
| agent 列表数据源 | 与 `Config`/`Agents` 扫描一致，供 `/agent` 选择器 |

### 9.3 保留

`Agent`（定义）、`Run`、`Session`、`ModelContext`、`ExecutionStrategy`/`ReActStrategy`、`SkillRegistry.discover`、`HookFeedback`、`Environ`、`Boot`（装载 Run，不拼 Request 全文）。

---

## 10. 分阶段落地

| 阶段 | 内容 | 验收 |
|------|------|------|
| **P0** | templates 外置 + 加载；文案与现网一致 | snapshot/单测 |
| **P1** | `prepare` + stages；ReAct 只调 prepare；skills 每 turn discover | pytest 全绿；同进程改 skill 下一 turn 生效 |
| **P2** | slash：`/model` `/thinking` `/agent`（列表+id）`/new` `/reload`；`before_request` | 手工 + 单测 |
| **P3** | Recall 接口；OV 挂 recalls | ReAct 无 OV 特判 |
| **后话** | `/fork-agent`、pi 式 user prompts、MemoryRecall | — |

---

## 11. 设计约束

1. Agent = 定义；Loop/Run + Session = 运行与日志。  
2. Request 只经 `prepare`。  
3. 编排器无业务 if；扩展用列表/窄接口。  
4. system 固定文案 = **templates**；勿与 pi **prompts** 混名。  
5. slash 少：配置资源靠 `/reload` + 静默；身份靠 `/agent` `/new`；试错靠 `/model` `/thinking`。  
6. `/agent` = **新开空 Session**；fork 非默认。  
7. 运行时不读 `config.yaml`；数据在 `~/.pickel`。  
8. 命名短直：prepare、resolve_*、templates、Recall。  

---

## 12. 审阅清单

- [x] Agent = 定义  
- [x] prepare 阶段表，非上帝 Build  
- [x] templates 外置；skills 每 turn 可热更  
- [x] `/reload` 范围（§6.3）  
- [x] slash 精简；含 `/thinking`  
- [x] `/agent`：id 或下拉列表；默认新开空 Session  
- [x] fork 非默认、后话  
- [ ] 实现计划与编码（审阅通过后）  

---

## 13. 小结

**prepare 阶段表** 统一组 Request；**templates** 管 system 固定字；**每 turn discover skills** + **一条 `/reload`** 管磁盘资源；**`/model` `/thinking`** 管会话 Environ；**`/agent`（名或列表）`/new`** 管会话身份（换 agent = 新空 Session）。  
对齐 pi 的 snapshot、`/model`、`/reload` 心智，并与当前 pickel 代码可映射、可分期落地。
