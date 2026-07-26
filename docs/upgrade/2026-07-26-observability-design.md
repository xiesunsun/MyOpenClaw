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
- 非用户可见的第二套平行对话事实库  

### 2.3 我们采取

| 学 | 不硬搬 |
|----|--------|
| usage 长在消息 metadata 上 | 平行 telemetry 对话库 |
| UI = 派生 | 估计值写回覆盖账单 usage |
| 上下文 = usage 锚 + 必要估计 | 假装 estimate = 账单 |
| 扩展读同一 measure/派生 API | 一上来分布式 tracing |

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
| before_request 改写后的 Request | 未记录 | 默认可观测「未跑 hook 的预览」；完整一致性靠日后摘要 |
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

pi 对照：占用 ≈ last usage + trailing estimate；合计 ≈ sum message usage。

我们：

- **A**：扫 path 上 assistant（等）metadata  
- **B**：`Request = prepare(...)`，再 `measure(Request)`  
- **并排展示，互不覆盖存储**

---

## 6. measure(Request) 算法合同

**输入：** 一份 `ModelContext`（Request），来自 prepare 预览（或 debug 快照）。  
**输出：** `ContextUsage` 值对象（不强制落盘）。

| 字段 | 算法 |
|------|------|
| `tokens(X)` | 优先 Provider `count_context_tokens`；失败则启发式（如 chars/4），`estimated=true` |
| Messages | 仅 messages 部分 |
| System base | 仅 behavior（或 base system）相对空上下文的增量 |
| Skills | full system − base system；可选 per-skill 差分 catalog |
| Tools | full(request with tools) − (system+messages) |
| Total | 完整 Request |
| Max / Free | `max_input_tokens − total`（无 max 则 Free 空） |
| Last API | **不来自 measure**；来自 Session last usage |

**空会话：** messages 栏为 0；仍可算 system + tools + total + 进度条。  

**副作用：** measure **默认**不触发 before_request、不远程 OV recall（与 harness 文一致）。需要时显式参数（后话）。

**实现注：** 现 `ContextUsageService` 仍依赖旧 `GenerateRequest` 形状，**应对齐 prepare/ModelContext 重写**；`ContextRenderer` 进度条分栏可复用为视图。

---

## 7. `/context` 产品合同

### 7.1 定义

**`/context` = ContextUsage 视图：**  
展示「若现在进入下一 step，prepare 将发出的 Request」的 **估计占用**，并在有数据时展示 **上一轮 API usage**。

### 7.2 目标 UI（逻辑）

```text
Context Usage
{provider} / {model}
{total} / {max} tokens   [####----] {pct}%
{estimated 标记}

By category
  System     …
  Skills     …   (+ 可选明细)
  Messages   …   （空会话为 0）
  Tools      …
  Free       …

Last API call (if any)
  duration_ms / in / out / cache_read / cache_write
  model=…

Source: prepare preview · hooks skipped · no draft input
```

### 7.3 非目标（本命令）

- 完整历史 Request 回放  
- 替代 `/session` 的会话元信息  
- 执行完整 before_request / 默认远程 recall  

### 7.4 与错误现状对照

| 现状问题 | 合同要求 |
|----------|----------|
| 只显示 sections/messages/tools 个数 | 主视觉为 **token 分栏 + 进度条** |
| 误报「无模型调用」式文案 | 有 prepare 结果即展示占用；无 usage 仅 Last 栏空 |
| 未用 ContextRenderer | O1 接回用量渲染 |

---

## 8. 命名（短、一事一物）

| 名 | 职责 |
|----|------|
| **Session / SessionEntry** | 对话真源（已有） |
| **Request** | 一次 model 入参（`ModelContext` 可保留类型名） |
| **measure** | Request → ContextUsage（无状态） |
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

**旁路 Trace 仅当：** 需要跨进程审计、或必须存「当时 Request 摘要」且不愿污染 entry 合同——且必须 **标注非对话真源**。

---

## 10. 分期

| 期 | 内容 | 真源 |
|----|------|------|
| **O0** | 本文合同审阅通过 | — |
| **O1** | 重写 measure（基于 prepare/ModelContext）；`/context` 接 ContextRenderer 式 UI + last usage | B 估计 + A 持久 |
| **O2** | 会话 usage 合计、只读轨迹摘要（纯 Session） | A |
| **O3** | metadata 补齐 step 所需字段（thinking 等） | 扩 A |
| **O4** | 可选 RequestDigest 旁路（debug 开关） | 显式非主真源 |

---

## 11. 与 prepare / Session 的耦合红线

1. 组 Request：**仅 prepare**（ReAct 与 `/context` 共用）。  
2. API usage：**仅**成功 generate 后写入 assistant metadata 再落盘。  
3. `/context` 不写 Session（只读 + 现态 prepare）。  
4. measure 失败 → 降级估计或显示 unknown，不编造与 prepare 不一致的 messages。  
5. OV 同步状态继续 bypass，不进 sessions 封面。  

---

## 12. 审阅清单

- [ ] 主真源 = Session entry + metadata.usage  
- [ ] 预览占用 = prepare + measure，标 estimated  
- [ ] 禁止双写 usage / 双路径拼装  
- [ ] `/context` = 用量分析 UI（对齐 pi 派生 + 旧 ContextRenderer）  
- [ ] 完整 Request 回放默认不做  
- [ ] 分期 O1 优先恢复 `/context` 分析，不先上独立观测库  

---

## 13. 小结

可观测性 = **会话真源 + 只读派生 + 少量可选 debug 旁路**。  
对齐 pi：usage 在消息上，UI 做合计与上下文估计。  
`/context` 是 **ContextUsage 视图**，用 prepare 保证与业务一致，用 measure 做分栏，用 metadata 展示真实 API usage——三者语义分离，避免事实不统一。
