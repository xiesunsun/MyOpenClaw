# 模型请求组装（prepare / Request）升级设计

**状态**：设计稿（待审阅）  
**分支**：`feature/context-request-prepare-design`  
**范围**：system / 历史 / skills 文案 / hook 反馈 / 可选召回 如何统一拼成一次 model 调用入参  
**不在范围**：记忆产品方案定稿、PlanExecute/Reflection strategy、包名与配置分层（已另文）

**相关实现（现状）**：`agents/skills.py`、`context/assembler.py`、`context/projection.py`、`runs/strategy/react.py`、`hooks/*`  
**配置域已定稿**：`docs/upgrade/2026-07-25-config-system-design.md`（Settings / Environ / Agent 定义等）

---

## 1. 目标

| 目标 | 说明 |
|------|------|
| 文案可改 | skills 引导语、catalog 行格式等外置，改字不改 Python |
| 组装唯一路径 | 发模型前只走固定阶段表，禁止 ReAct/Agent 各拼一段 |
| 避免上帝类 | 编排器极薄；每阶段小模块/纯函数 |
| 扩展有槽 | OpenViking recall、未来记忆挂窄接口，不塞进 loop 特判 |
| 语义清晰 | Agent = 定义；Loop/Run = 运行；Session = 日志；Request = 一次调用入参 |

**非目标**

- 把 Agent 改成活的运行主体  
- 用巨型 `Build` / `Mount` 包揽一切  
- 强行 process/thread 隐喻套 turn/query  

---

## 2. 现状问题

```text
AGENT.md + SKILL_USAGE_GUIDANCE(硬编码) + catalog
        → Agent.system_instruction
                ↓
ReAct: SystemContent.from_text(...)
     + project_messages + window
     + hook_feedback 尾部 user
     + tools
                ↓
         ModelContext → Provider
```

| 痛点 | 表现 |
|------|------|
| 文案焊死 | `SKILL_USAGE_GUIDANCE` 在 `skills.py` |
| 拼接点分散 | Agent 属性 / ReAct / Assembler / projection |
| Hook 只管执行 | 有 Pre/Post tool、UserPromptSubmit；**无**「改即将发送的 Request」 |
| 扩展无标准槽 | OV / 记忆易变成 ReAct if 或旁路特判 |

---

## 3. 概念模型（审阅重点）

### 3.1 实体与真假

| 名字 | 是什么 | 实体性 |
|------|--------|--------|
| **Agent** | 角色**定义**（`agents/<id>/` + 解析结果） | **配置型概念实体**，不是活主体 |
| **Run** | 某定义装载后的**资源袋**（provider/tools/hooks/environ…） | 运行时资源，**不是** OS 进程 |
| **Loop** | 执行回路（现：`ExecutionStrategy` + `Run.turn`） | 运行时控制流；多 Agent ≈ 多 Loop，可共用库 |
| **Turn** | 一轮用户输入到终答（可含多 step） | 作业/事务语义，**不是**线程 |
| **Step** | 一次 model 调用 | 瞬时 |
| **Request** | 一次 model 入参（现 `ModelContext` 可保留类型名） | 瞬时值对象 |
| **Session** | 对话日志（entry 树） | 持久实体 |
| **Prompts** | 可覆盖文案模板 | 文件实体 |
| **Hooks** | 执行边界 + 可选改 Request | 运行时策略 |
| **Recall** | 可选召回源（OV/记忆） | 窄扩展接口 |

**Agent 与主流 harness 对齐**：pi / Claude Code 等 coding harness 多以 Session+Loop 为核心；有 `Agent` 类型时（如部分 SDK）也是 **spec/配置包**，不是进程。本设计**保留 Agent 名，语义固定为定义**。

### 3.2 不宜再强调的隐喻

| 弱化 | 原因 |
|------|------|
| Run = 进程 | 多 Agent 时误导；Run 是资源袋 |
| query/turn = 线程 | 无调度/共享内存语义 |
| 万能 Build / Mount | 易成上帝类 |

| 保留 | 原因 |
|------|------|
| 配置域 Settings/Environ 等 OS 词 | 已与 `config-system-design` 一致 |
| 固定阶段 + 小模块 | 对齐 Unix 管道/内核槽位，也对齐 pi 的 turn snapshot |

### 3.3 总览图

```text
磁盘定义                    运行时
agents/<id>/ ──► Agent ──► Run（资源）+ Loop（策略）
                              │
Session（日志）───────────────┤
Prompts（文案）───────────────┤
Recall 源列表（可选）─────────┤
                              ▼
                     prepare（薄编排）
                       ├ resolve_system
                       ├ resolve_history
                       ├ resolve_recalls
                       ├ resolve_feedback
                       ├ resolve_tools
                       └ hooks.before_request
                              ▼
                          Request ──► Provider.generate
                              │
                              ▼
                     Session append（日志）
```

多定义并排：

```text
Agent 定义 Pickle  ──► Loop_P + Session…
Agent 定义 Architect ──► Loop_A + Session…
共享：Config / Models / Auth / ~/.pickel/sessions.db
```

---

## 4. 横向参考（为何不用上帝 Build）

### pi

- **Harness config** vs **Turn snapshot** 分离：中途改配置只影响下一 turn  
- **小函数**拼 skills 文案（如 `formatSkillsForSystemPrompt`），独立文件  
- **resources** 外置加载，注入 harness，不在 loop 里扫盘拼字  
- extension/hooks 在边界改行为  

### Claude 类 coding agent

- 分层 md / skills 渐进披露  
- Hooks 拦 tool/输入，**不**替代整条组装管道  
- 内部仍是固定阶段  

### 本设计采取

| 做法 | 对应 |
|------|------|
| Turn/Step 快照式 Request | pi `createTurnState` |
| 有序 stage，每 stage 一模块 | 管道，非上帝类 |
| Prompts 文件覆盖 | 配置域同款 defaults < user < project |
| 窄 Recall 接口列表 | 非万能 Mount |
| 执行 Hooks + `before_request` | 边界拦截 + 可选改 Request |

---

## 5. prepare：薄编排 + 阶段表

### 5.1 入口

```text
prepare(run, session, *, hook_feedback=None, step_meta=None) -> Request
```

- **唯一**允许从 Session+Run 生成发模型内容的路径  
- ReAct **禁止** 再 `SystemContent.from_text(agent.system_instruction)` 私自拼 system  
- 编排器只按序调用 stages，**自身无业务分支**（无 if openviking / if memory）

### 5.2 阶段表（固定顺序）

| 序 | 阶段 | 职责 | 默认实现来源 | 可扩展 |
|----|------|------|--------------|--------|
| 1 | `resolve_system` | behavior + Prompts + skills catalog | 现 compose + 外置文案 | SystemSection 列表（可选） |
| 2 | `resolve_history` | entry → messages + window | projection + window | 策略参数（window） |
| 3 | `resolve_recalls` | 注入召回消息/段 | 空列表 | **Recall** 实现 |
| 4 | `resolve_feedback` | hook 文本 → 尾部 user | 现 `append_hook_feedback` | — |
| 5 | `resolve_tools` | tools → ToolDefinition | 现 ReAct 内映射 | ToolFilter（可选） |
| 6 | `before_request` | Hooks 可改 Request | 新增 | 各 handler |

`ContextAssembler`：演进为 history+feedback 的默认组合，或拆入 2/4；对外只暴露 `prepare`。

### 5.3 为何不会变成上帝类

```text
prepare.py          # 10～20 行 for stage in stages
system.py           # 只拼 system
history.py          # 只投影+窗口
recalls.py          # for source in recall_sources
feedback.py         # 已有逻辑搬家
tools_stage.py      # 映射 tools
```

新增能力 = **新 stage 文件** 或 **往 recalls 列表加实现**，不改 prepare 主体。

---

## 6. Prompts（文案外置）

```text
包内 pickel/prompts/          # 默认（现硬编码迁出）
~/.pickel/prompts/            # 用户覆盖
项目/.pickel/prompts/         # 项目覆盖
```

合并：默认 < 用户 < 项目（与 Settings 一致）。

| 建议文件 | 替代 |
|----------|------|
| `skills_guidance.md` | `SKILL_USAGE_GUIDANCE` |
| `skill_catalog_line.md` 或纯函数+模板 | `format_skill_catalog_entry` |

第一期：迁出后 **默认字符串与现网一致**（行为不变）。

---

## 7. 窄扩展：Recall（取代万能 Mount）

```text
class Recall(Protocol):
    def provide(self, *, run: Run, session: Session, ...) -> list[Message|Section]:
        ...
```

| 实现 | 用途 |
|------|------|
| （无） | 默认 `recall_sources = []` |
| OpenVikingRecall | session recall 进 Request |
| MemoryRecall | 后话；本设计只占位 |

**禁止** 统一 `Mount` 同时贡献 system/history/tools/auth。  
若未来要改 tools 可见性，另设 `ToolFilter`，不并进 Recall。

---

## 8. Hooks 两层

| 层 | 事件 | 职责 |
|----|------|------|
| 执行层（已有） | user_prompt_submit、pre/post_tool、post_tool_batch、turn_end | 拦输入/工具、feedback 文本 |
| **请求层（新增）** | **before_request** | 入参拟发送 Request；可返回修改后的 Request 或附加 feedback |

约束：

- 长文案不进 hook 硬编码；走 Prompts  
- OpenViking **同步/游标** 仍旁路；**进模型的召回** 走 Recall 或 before_request，不进 ReAct 特判  

---

## 9. 与现类型映射

| 现有 | 目标 |
|------|------|
| `Agent` | 保留；文档与代码注释标明 **定义/profile** |
| `Agent.system_instruction` | 改为原料访问；全文由 `resolve_system` 生成（可保留 property 委托 prepare 的 system 段，避免双源） |
| `compose_system_instruction*` | 迁入 `resolve_system` + Prompts |
| `ContextAssembler` | prepare 内部 |
| `ModelContext` | 即 Request（类型名可暂不改） |
| `LifecycleHooks` | + `before_request` |
| `SessionRecallProvider` | 实现 `Recall` |
| `ReActStrategy` | 每 step 调 `prepare`；不拼文案 |
| `Run` | 资源袋；语义非进程 |
| `ExecutionStrategy` | Loop 的策略实现；保留扩展 PlanExecute 等 |

---

## 10. 需求落点

| 需求 | 改哪里 |
|------|--------|
| 改 skill 与 system 拼接字 | Prompts 文件 |
| 改 catalog 行格式 | 模板或 `system` 小函数 |
| 历史裁剪 | `resolve_history` / unit_window |
| 工具结果形态 | 优先不改历史 payload；展示层后话 |
| user 包装 | user_prompt_submit feedback |
| OpenViking 进模型 | Recall 源 |
| 记忆 | MemoryRecall 后话；默认空列表 |

---

## 11. 分阶段落地

| 阶段 | 内容 | 验收 |
|------|------|------|
| **P0** | Prompts 外置 + 加载合并；默认文案 = 现状 | 单测/snapshot 与现输出一致 |
| **P1** | `prepare` + stages；ReAct 只调 prepare | 全量 pytest 绿；行为对齐 |
| **P2** | `before_request` hook | 测试可改 Request |
| **P3** | `Recall` 接口；OV 挂 recalls | ReAct 无 OV 特判 |
| **后话** | MemoryRecall、ToolFilter、Turn 级 snapshot 与 config 热改隔离（对齐 pi） | — |

---

## 12. 设计约束

1. Agent 是定义，不是运行主体；运行主体是 Loop/Run + Session。  
2. 发模型内容只经 `prepare` 阶段表。  
3. 编排器不写业务 if；扩展进列表/窄接口。  
4. 文案优先 Prompts 文件。  
5. 不引入 ConfigRuntime / ContextManager / 万能 Mount 等空壳层名。  
6. 与配置域一致：运行时不读 `config.yaml`（仅 migrate）。  
7. 数据路径：`~/.pickel`；项目旁 `.myopenclaw` 为历史残留，勿再使用。  

---

## 13. 审阅清单

请重点拍板：

1. **Agent = 定义** 的表述与命名是否保留 `Agent`  
2. **prepare + 固定 stages** 是否接受（反对上帝 Build）  
3. **Recall 窄接口** 是否接受（反对万能 Mount）  
4. **before_request** 是否纳入 P2  
5. **P0 是否必须行为字节级兼容** 现 system 字符串  

审阅通过后再开实现计划与编码。

---

## 14. 小结

用 **定义（Agent）+ 资源（Run）+ 回路（Loop/Turn/Step）+ 薄 prepare 阶段表 + Prompts + 窄 Recall + 双层 Hooks**，替换「硬编码拼接 + 分散组装」。  
对齐 pi 的 snapshot/小函数思路与主流 coding harness 的 Session 中心模型；避免 process/thread 硬套与上帝类。
