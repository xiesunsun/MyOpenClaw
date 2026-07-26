# 运行可观测性（Observability）设计

**状态**：设计稿（待审阅）  
**分支**：`feature/context-request-prepare-design`  
**范围**：对话运行的真源划分、派生用量、`/context` 合同、与 prepare/Session 的耦合  
**不在范围**：分布式 tracing 平台、记忆产品、PlanExecute 策略、配置分层（另文）

**关联**

| 文档 | 关系 |
|------|------|
| `2026-07-12-db-entities.md` | Session / entry 持久合同 |
| `2026-07-12-query-context-harness.md` | ModelContext、`/context` 早期合同（usage + 不副作用） |
| `2026-07-25-request-prepare-design.md` | prepare 唯一组 Request；slash 边界 |
| 代码 | `session_entries` payload、`ModelResponseMetadata`、`prepare`、`ContextUsageService`（待对齐）、`cli/chat.py` `/context` |

---

## 1. 目标

| 目标 | 说明 |
|------|------|
| 事实统一 | 对话与调用结果 usage **只认 Session entry**；禁止平行「第二套对话/usage 库」当主真源 |
| 可扩展 | 为 query 记录、轨迹、Request/Response、step 配置、duration 留清晰扩展位 |
| `/context` 正确定义 | **用量分析视图**（估计 + 可选上次 API usage），不是结构计数字段 dump |
| 低耦合 | 业务路径 emit 少而固定；视图只读派生；失败不拖垮 turn |

**非目标**

- 默认持久化完整 wire Request 全文  
- 为每个 slash 单独拼装路径  
- ObservabilityManager / 遥测中台命名与架构  

---

## 2. 标杆对照（pi / Claude 类）

### 2.1 pi coding-agent

| 点 | 做法 |
|----|------|
| 真源 | 会话 JSONL 树（message / compaction / …） |
| usage | **写在 assistant（等）消息上**，含 input/output/cache/cost |
| 会话合计 | 扫 entry **加总** message.usage（`usage-totals`） |
| 当前上下文占用 | **最近有效 assistant.usage** + **其后消息的 estimate**；compaction 后可暂 `?/window` |
| UI | Footer、`/session`、扩展 `getContextUsage()`：**同一套派生** |
| 独立观测库 | **无**；可观测 = 日志 + 派生视图 |

### 2.2 Claude Code（产品习惯）

- 以会话/transcript 为中心  
- 窗口与 usage 感来自 **调用结果 + 本地估计**  
- **无**非用户可见的第二套平行对话事实库  
- **分栏 UI**（System / Tools / Memory / Messages / Free）是我们 §7.2 的形态来源；其内部算法未公开，**不作为实现依据**  

### 2.3 我们采取

| 学 | 不硬搬 |
|----|--------|
| usage 长在消息 metadata 上 | 平行 telemetry 对话库 |
| UI = 派生 | 估计值写回覆盖账单 usage |
| **占用锚 = 真实 last usage + 尾部估计**（pi 法） | 每次观测都远程 count_tokens |
| 扩展读同一 measure/派生 API | 一上来分布式 tracing |

**与 pi 的唯一有意偏离：** pi 在锚失效（compaction 等）后显示 `?/window`；我们**多一档远程 `count_context_tokens` 兜底**，让首轮 / 空会话 / compaction 后仍有数（见 §6.1）。

---

## 3. 真源划分（审阅重点）

### 3.1 原则

1. **一种业务事实一个家。**  
2. **能从 Session 唯一还原的，只从 Session 读。**  
3. **派生可丢、可重算；真源不可双写不一致。**  
4. **预览/估计必须在 UI 与字段语义上可区分于 API usage。**  

### 3.2 可从现有持久化获取（主真源：Session）

| 数据 | 来源 | 用途例 |
|------|------|--------|
| 对话树、活动路径 | `sessions` + `session_entries` | 轨迹、query 列表 |
| user / assistant / tool 正文 | message payload | 回放、展示 |
| tool 调用与结果 | entry 链 | 执行轨迹 |
| provider / model（该次回复） | `AssistantMessage.metadata` | step 模型配置（部分） |
| usage（in/out/cache/…） | `metadata.usage` | 账单侧、Last API、会话合计 |
| elapsed_ms | `metadata.elapsed_ms` | 耗时 |
| finish_reason 等 | metadata | 诊断 |
| agent_id、cwd、时间 | session 封面 | 列表、过滤 |
| compaction 摘要与边界 | compaction entry | 压缩后路径理解 |

→ **query 记录、对话级执行轨迹、模型回复、该次回复的 usage/model/ms：只读 Session，不另建主表抄写。**

### 3.3 不能从持久化唯一还原（禁止假装「已经有」）

| 数据 | 原因 | 策略 |
|------|------|------|
| 历史某次 **完整 wire Request** | 未落库；reload skills/templates 后 prepare 结果会变 | 默认不做回放；debug 可选 RequestDigest（显式非主真源） |
| 当时 Environ/thinking（若未写入 metadata） | 未进 entry | 需要则 **扩 metadata 写入**（仍是 Session 真源），不平行抄 |
| 当时完整 tools 白名单 | 轨迹仅含调用到的 tool | 同上或接受不可得 |
| before_request 改写后的 Request | 未记录 | 默认可观测「未跑 hook 的预览」；**O1d 起在 metadata 记 `hook_injected_chars`**，使偏差可发现（§9） |
| **下一 step** 用量分栏 | 尚未发生 | **现态 prepare + measure**，标 `preview` / `estimated` |
| system vs skills vs tools 分栏 token | entry 无分段计量 | measure 派生，可丢弃 |

### 3.4 事实不统一的禁止项

| 禁止 | 说明 |
|------|------|
| 两套拼装 | `/context` 与 ReAct 必须共用 **prepare** |
| 双写 usage | Trace 表与 entry.metadata 各记一套且不一致 |
| 估计覆盖账单 | measure 结果写入 `metadata.usage` |
| Session 封面塞 OV 游标/同步 | 继续旁路表（已有合同） |

---

## 4. 分层架构

```text
业务路径
  Run / prepare / Provider / Tools
        │
        │  调用结果 usage 等写入 AssistantMessage → Session（真源）
        │  （可选 debug）Request 摘要旁路
        ▼
只读派生
  sum(usage) · last_usage · 轨迹序列
  measure(Request) → ContextUsage（估计，可丢）
        ▼
视图
  /context · 将来 /trace · 导出 · footer
```

| 层 | 职责 | 禁止 |
|----|------|------|
| 业务 | 正确 turn/step | 为 UI 分叉拼装 |
| Session | 对话与调用结果事实 | 塞满可丢弃的估计字段当合同 |
| 派生 | 合计、预览用量 | 反向改 leaf/entry 正文 |
| 视图 | 渲染 | 私自组 ModelContext |

---

## 5. 两类用量（必须分开）

| 类型 | 名称建议 | 真源 | UI 标注 |
|------|----------|------|---------|
| **A. 调用结果** | Last / Session **API usage** | entry `metadata.usage`（及将来 tool 内嵌若有） | 真实 / reported |
| **B. 上下文占用** | **Context usage（preview）** | 现态 `prepare` → Request → `measure` | estimated / preview |

pi 对照：占用 ≈ last usage + trailing estimate；合计 ≈ sum message usage。**我们采用同一锚定法**（见 §6.1），不每次远程 count。

我们：

- **A**：扫 path 上 assistant（等）metadata  
- **B**：`Request = prepare(...)`，再 `measure(Request, anchor)`  
- **并排展示，互不覆盖存储**

### 5.1 「输入规模」的展示口径（强制）

Anthropic 的 `input_tokens` **不含** `cache_read` / `cache_write`。任何把 `input_tokens` 单独当作「本次输入总量」的展示或计算，在开启 prompt cache 时会低估到十分之一量级。

```
实际输入规模 = input_tokens + cache_read_tokens + cache_write_tokens
```

- UI 必须给出这个合计值，四个分项可并列展示但不得替代合计  
- §6.1 的锚也必须用这个合计值，不能用裸 `input_tokens`

---

## 6. measure 算法合同

**输入：** 一份 `ModelContext`（Request，来自 prepare 预览）+ 一个可选 `UsageAnchor`（来自 Session）。  
**输出：** `ContextUsage` 值对象（不强制落盘）。

设计要点：**total 靠锚（真实 usage）拿准，分栏靠本地估计拿快。** 两者职责分离，永不互相污染。

### 6.1 total 的三档来源（按序尝试）

| 档 | 条件 | total | 远程调用 | 标注 |
|----|------|-------|----------|------|
| **A 锚命中** | 有 last usage；其后无新消息；fingerprint 未变 | `anchor` | 0 | `measured` |
| **B 锚 + 尾部** | 有 last usage；其后有新消息；fingerprint 未变 | `anchor + estimate(尾部消息)` | 0 | `estimated` |
| **C 兜底** | 无锚或锚失效 | `count_context_tokens(request)` | 1 | `measured`；失败则纯本地估计并标 `estimated` |

**anchor 定义：** active_path 上最近一条 `AssistantMessage.metadata.usage` 的
`input_tokens + cache_read_tokens + cache_write_tokens`（见 §5.1，不得用裸 `input_tokens`）。

**锚失效条件（任一即回落到 C）：**

- 该 assistant 之后出现 compaction entry  
- fingerprint 变化：provider / model / agent_id / system 文本 hash / tools 集合 hash  
  （即 `/model`、`/agent`、`/reload`、skills 目录变更后锚一律作废）  
- 无任何 assistant entry（首轮 / 空会话 / `/new` 之后）

**为什么不是「每次远程 count」：** 未来 footer 需逐轮实时显示占用，远程往返在那个位置不可用。现在按锚定法定合同，footer 才能直接复用同一 measure，不重写。

### 6.2 分栏算法（一律本地估计）

```
raw[i] ← estimate(section_i) | estimate(messages) | estimate(tools)
scale  ← total / sum(raw)          # sum(raw) 为 0 时跳过归一化
栏位[i] ← round(raw[i] * scale)
Other  ← total - sum(栏位)         # 归一化残差与 provider 固定开销的去处
```

| 栏 | 来源 |
|----|------|
| System | `SystemSection(name="behavior")` |
| Skills guidance | `SystemSection(name="skills_guidance")` |
| Skills catalog | `SystemSection(name="skills_catalog")`；per-skill 明细同样本地估计，**不做远程差分** |
| Messages | `context.messages` |
| Tools | `context.tools` |
| Other | 归一化残差（含 provider 固定开销） |
| Free | `max_input_tokens − total` |

**强制约束：**

- 栏位**不得为负**，截断到 0  
- 栏位之和 + Other **恒等于** total；不一致时以 **total 为准**  
- 分栏永远标 `estimated`，即便 total 是 `measured`  
- `estimate()` 为本地启发式（如 chars/4）或本地 tokenizer，**不发起任何网络请求**

**前置改动（否则本节不可实现）：** 现 `resolve_system` 用 `SystemContent.from_text(parts.full_instruction)` 只产 1 个 section，Request 内部无边界可拆。须改为按 `SystemInstructionParts` 已有的三段直接映射为 `SystemSection`：
`behavior` / `skills_guidance` / `skills_catalog`（`SystemSection.name` 字段本就为此保留）。

**零风险论证：** `SystemInstructionParts.full_instruction` 与 `SystemContent.as_text()` 都是「过滤空串后 `"\n\n".join`」，同序同分隔符 → 改造后 provider 收到的 system 文本**逐字节相同**，可用一条等价性测试锁死。

### 6.3 其它

| 字段 | 算法 |
|------|------|
| Max / Free | `max_input_tokens − total`（无 max 则 Free 与进度条显示 unknown） |
| Last API | **不来自 measure**；来自 Session last usage |

**`max_input_tokens` 口径：** 取自 `ModelConfig.max_input_tokens`，是**配置值**，非 provider 实测；不含 output 预留，因此进度条满格 ≠ 立即溢出。缺省为 `None` 时进度条显示 unknown，不猜测。

**空会话：** messages 栏为 0。**不得**为了凑 A 档/C 档而向 provider 发空 messages 请求——Anthropic `count_tokens` 要求 messages 非空会直接报错。空会话一律走本地估计（`estimated=true`）。

**副作用：** measure 与 `/context` 预览路径 **不触发 before_request、不执行 recall（含远程 OV）、不发起除 C 档外的任何网络请求**。C 档每次观测至多 1 次远程调用。

**实现注：** 现 `runs/context_usage.py` 依赖已被删除的 `Provider.count_request_tokens` 与旧 `GenerateRequest`，**import 即坏**，对应测试已全 skip。O1 直接**删除并按本节重写**，不做增量修补；`ContextRenderer` 的进度条与分栏视图可复用。

---

## 7. `/context` 产品合同

### 7.1 定义

**`/context` = ContextUsage 视图：**  
展示「若现在进入下一 step，prepare 将发出的 Request」的 **估计占用**，并在有数据时展示 **上一轮 API usage**。

> **本节取代** `2026-07-12-query-context-harness.md` §15 中「`/context` 优先读取最近一次**真实的** final ModelContext」的条款。
> 理由见 §3.3：历史 wire Request 未落库，reload skills / 切 model 后不可还原，"读取最近一次真实 ModelContext" 无法实现。
> `/context` 的语义自本文起为**下一 step 预览**，不是回看。usage 部分仍是真实值（来自 Session）。

### 7.2 目标 UI（逻辑）

```text
Context Usage
{provider} / {model}
{total} / {max} tokens   [####----] {pct}%
{measured | estimated}          ← total 来源，见 §6.1

By category                     ← 一律 estimated
  System     …
  Templates  …
  Skills     …   (+ 可选明细)
  Messages   …   （空会话为 0）
  Tools      …
  Other      …
  Free       …

Last turn (if any)              ← 本轮全部 step 合计，不是单次
  steps={n}  duration_ms={sum}
  实际输入={in + cache_read + cache_write}   out={sum}
  明细: in=… cache_read=… cache_write=…
  model=…

Session total (if any)
  实际输入={…}  out={…}

Source: prepare preview · hooks skipped · recall skipped · no draft input
```

**`Last turn` 而非 `Last API call`：** ReAct 一个 turn 可能多次 generate，产生多条 assistant entry。只显示最后一次 step 会让用户系统性低估本轮成本，必须按 turn 合计。

### 7.3 非目标（本命令）

- 完整历史 Request 回放  
- 替代 `/session` 的会话元信息  
- 执行 before_request  
- **执行任何 recall（含远程 OV）** —— 预览恒小于实际请求，须在 Source 行注明 `recall skipped`

### 7.4 与错误现状对照

| 现状问题 | 位置 | 合同要求 |
|----------|------|----------|
| 只显示 sections/messages/tools 个数 | `cli/context_renderer.py` `ModelContextRenderer` | 主视觉为 **token 分栏 + 进度条** |
| 误报「无模型调用」式文案 | `cli/chat.py` `_render_context_command` | 有 prepare 结果即展示占用；无 usage 仅 Last 栏空 |
| 未用 ContextRenderer | 同上 | O1 接回用量渲染 |
| **传入 `run.recall_sources` → 触发远程 OV recall** | `cli/chat.py:517` | 改传 `[]`，违反 §7.3 |
| **`unit_window` 硬编码 fallback `5`** | `cli/chat.py:516` | 用 `run.unit_window`，否则预览与实际请求不同窗口 |
| **last usage 存在 `_last_assistant_metadata` 内存变量** | `cli/chat.py:588` | 改为从 `session.active_path()` 反查；内存态重启即失，且违反 §3.1「能从 Session 唯一还原的只从 Session 读」 |

---

## 8. 命名（短、一事一物）

| 名 | 职责 |
|----|------|
| **Session / SessionEntry** | 对话真源（已有） |
| **Request** | 一次 model 入参（`ModelContext` 可保留类型名） |
| **measure** | (Request, UsageAnchor?) → ContextUsage（无状态、无网络除 §6.1 C 档） |
| **UsageAnchor** | 从 Session 派生的真实 usage 锚 + 失效判据（§6.1） |
| **ContextUsage** | 估计占用快照（值对象） |
| **prepare** | 业务唯一组 Request（已有设计） |
| **Trace / Record**（后话） | 仅当 Session 不够且产品需要时；**引用 entry_id**，不抄正文 |

禁止：ObservabilityManager、双路径 Assembler、静默双写 usage。

---

## 9. 扩展能力与真源映射（前瞻）

| 产品能力 | 真源策略 |
|----------|----------|
| query 记录 | Session：时间序 user entry + preview |
| 执行轨迹 | Session active_path 类型序列 + tool 对 |
| 模型回复 | assistant entry |
| step model 配置 | metadata.provider/model；缺 thinking 则 **扩 metadata 写入** |
| usage / cache | metadata.usage |
| duration_ms | metadata.elapsed_ms |
| chat-completion 请求全文 | **默认不持久**；debug RequestDigest 可选 |
| 下一 step 占用 | prepare + measure |
| before_request 改写量 | **metadata 新增 `hook_injected_chars`**（见下） |

**`hook_injected_chars`（O1 落地，不等 O4）：** before_request 已可改写 Request，而改写量当前完全不可观测——`/context` 预览会系统性偏离实际发送量且无从发现。在 `ModelResponseMetadata` 加一个整型字段记录 hook 注入的字符数，代价极低即可堵住这个盲点。`_metadata_from_dict` 走 `.get`，加带默认值的字段对既有 entry 向后兼容。

**旁路 Trace 仅当：** 需要跨进程审计、或必须存「当时 Request 摘要」且不愿污染 entry 合同——且必须 **标注非对话真源**。

---

## 10. 分期

| 期 | 内容 | 真源 |
|----|------|------|
| **O0** | 本文合同审阅通过 | — |
| **O1a** | `resolve_system` 产出 behavior/templates/skills 三段 `SystemSection`（§6.2 前置，对外行为不变，可独立合入） | — |
| **O1b** | 删除 `runs/context_usage.py` 与全 skip 的 `tests/runs/test_context_usage.py`，按 §6 重写 measure + UsageAnchor | B 估计 + A 持久 |
| **O1c** | `/context` 修 §7.4 四项（recall/window/内存 metadata/渲染），接 ContextRenderer 式 UI | A + B |
| **O1d** | `ModelResponseMetadata` 加 `hook_injected_chars`（§9） | 扩 A |
| **O2** | 会话 usage 合计、只读轨迹摘要（纯 Session）；footer 复用同一 measure | A |
| **O3** | metadata 补齐 step 所需字段（thinking 等） | 扩 A |
| **O4** | 可选 RequestDigest 旁路（debug 开关） | 显式非主真源 |

顺序 `O1a → O1b → O1c → O1d`，每步独立可测。O1a 不改任何对外行为，可先单独合入。

---

## 11. 与 prepare / Session 的耦合红线

1. 组 Request：**仅 prepare**（ReAct 与 `/context` 共用）。  
2. API usage：**仅**成功 generate 后写入 assistant metadata 再落盘。  
3. `/context` 不写 Session（只读 + 现态 prepare）。  
4. measure 失败 → 降级估计或显示 unknown，不编造与 prepare 不一致的 messages。  
5. OV 同步状态继续 bypass，不进 sessions 封面。  
6. `/context` 预览路径 **不执行 recall**（含远程 OV）、不执行 before_request。  
7. measure 除 §6.1 C 档外 **不发起网络请求**；分栏永不远程。  
8. last usage **只从 Session active_path 派生**，不缓存进 CLI 实例状态。  
9. 分栏之和 + Other **恒等于** total；栏位截断到非负。  

---

## 12. 审阅清单

- [ ] 主真源 = Session entry + metadata.usage  
- [ ] 预览占用 = prepare + measure，标 estimated  
- [ ] total 走 §6.1 三档锚定，默认零远程调用  
- [ ] 锚 = `input + cache_read + cache_write`，不是裸 `input_tokens`  
- [ ] 分栏一律本地估计并归一化到 total，非负、和恒等  
- [ ] `resolve_system` 先拆三段 section（O1a），否则分栏不可实现  
- [ ] `/context` 不执行 recall、不硬编码 window、last usage 从 Session 派生  
- [ ] `Last turn` 按 turn 合计，不显示单次 step  
- [ ] 禁止双写 usage / 双路径拼装  
- [ ] `/context` = 用量分析 UI（对齐 pi 派生 + 旧 ContextRenderer）  
- [ ] 完整 Request 回放默认不做；本文取代 harness §15  
- [ ] 分期 O1 优先恢复 `/context` 分析，不先上独立观测库  

---

## 13. 小结

可观测性 = **会话真源 + 只读派生 + 少量可选 debug 旁路**。  
对齐 pi：usage 在消息上，UI 做合计与上下文估计，**占用靠真实 usage 锚 + 尾部本地估计**，不靠反复远程 count。  
`/context` 是 **ContextUsage 视图**，用 prepare 保证与业务一致，用 measure 做分栏，用 metadata 展示真实 API usage——三者语义分离，避免事实不统一。

**一句话记住合同：** total 靠锚拿准，分栏靠估计拿快，两者都不写回 Session。
