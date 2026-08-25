# Agent Runtime 重构实施计划

**日期**：2026-08-24  
**状态**：实施中；v10 数据闭包与 Runtime 执行核心已接线  
**范围**：Runtime、Context、Operation 恢复、执行身份、持久化、Extension 生命周期与 Agent Delegation 的分阶段重构  
**不在范围**：Lane、通用事件溯源、Workspace 聚合根、完整插件框架、新界面功能

本文只规定实施顺序、批次边界和验收门槛，不重复定义领域实体。具体名称和语义遵循：

- [`Agent Runtime 重构命名约束`](./2026-08-10-agent-runtime-naming.md)
- [`数据库实体设计`](./2026-07-12-db-entities.md)
- [`配置系统升级设计`](./2026-07-25-config-system-design.md)
- [`Operation 持久化与恢复模型`](./2026-08-11-operation-recovery-model.md)
- [`Anthropic Provider 设计`](../superpowers/specs/2026-04-21-anthropic-provider-design.md)

逐项确认的实体边界记录在 [`Runtime 实体决策`](./2026-08-24-runtime-entity-decisions.md)。

四份领域合同已经统一到 Runtime 实体决策：目标持久化为 SQLite v10 明确领域表，当前状态使用 revision CAS；`ImmutableObject + NamedReference + StorageCommit` 只作为 v9 迁移来源。

## 1. 重构目标

本轮重构解决六个问题：

1. Runtime 不再成为同时承担 Context、Provider、Tool Loop、状态、持久化、Hook、事件和错误处理的 God Class。
2. 模型请求只有一条 Context 准备管道，Provider 不重复组装历史。
3. Session 不只恢复对话，还能判断并恢复或协调未完成 Operation。
4. 请求、步骤、工具和观测统一使用稳定执行身份。
5. Artifact、Agent Package 和 Agent Delegation 具备可恢复的底层承载。
6. Extension 的所有贡献和外部资源都能在失败、卸载和 reload 时撤销。

当前不引入 Lane。ConversationSession 保持一棵 Conversation Tree 和一个 `active_node_id`；多 Agent 并行通过独立 child Session 和 AgentDelegation 承载。只有出现“同一个 Session 内多个持久化执行游标同时推进”的明确需求后，才重新设计 Lane。

## 2. 实施原则

1. 一个批次只解决一个问题，必须可以独立合并和独立回滚。
2. 先建立失败用例，再修改实现。
3. 新路径接通后删除旧路径，不长期维护双轨生产实现。
4. 不为后续阶段预留公共 Manager、Coordinator、Lane 或通用资源袋。
5. 一个 ConversationSession 同时最多接受一个非终态 AgentRun Operation；如果该约束需要改变，必须先讨论并更新本文。
6. 每个批次完成时执行相关测试、全量测试、覆盖率和格式检查。
7. 实现和领域合同冲突时先停下讨论，不静默选择一方。

## 3. 目标组件边界

```text
RuntimeHost
  负责进程生命周期、RuntimeGeneration 和 Extension reload
        │
        ▼
AgentRegistry
  单进程 session_id → live Agent 唯一映射、引用和唤醒
        │
        ▼
Agent
  Root/Child 平等的 followup / steer / inject / cancel / when_idle
        │
        ▼
AgentDriver
  消费持久化 Inbox，接受或恢复 Operation
        │
        ▼
OperationDriver
  唯一 Agent Tool Loop
    ├── AgentRunStateMachine
    ├── ModelContextBuilder.build_model_context()
    └── RuntimeEffects
          ├── Provider
          ├── ToolExecutor
          ├── Hook / Recall
          └── Timer / HostCall
```

继续保留 `AgentDriver + OperationDriver + AgentRunStateMachine + RuntimeEffects` 的拆分，不增加新的有状态 Manager 或 Coordinator。`ConversationRuntime` 迁移期间只作为 Host/UI Adapter，不再次实现 Context、状态转换或 Tool Loop；无独立职责后删除。

## 4. 阶段总览

| 阶段 | 目标 | 完成标志 |
| --- | --- | --- |
| 0 | 建立可信回归基线 | 新环境能直接执行测试，六组架构护栏按实现批次落地 |
| 1 | 修复明确的正确性缺陷 | Extension 无半激活，Session 删除语义一致 |
| 2 | 闭合 Operation 恢复主链 | 重启后能发现并处理一个未完成 Operation |
| 3 | 收敛 Context | Provider、Trace 和 `/context` 对实际请求一致 |
| 4 | 统一执行身份 | 请求、Step、ToolCall 和观测可以完整串联 |
| 5 | 压平 Persistence | 经决策后，用明确领域事务替代无实际需求的通用抽象 |
| 6 | 完成 Multi-Agent 最小闭环 | child Session 可异步执行、取消和恢复 |
| 7 | 清理和验收 | 删除未接线公共能力，代码与命中文档一致 |

### 4.1 当前进度（2026-08-25）

| 批次 | 状态 | 已完成 | 尚未进入 |
| --- | --- | --- | --- |
| 0.1 测试入口 | 完成 | `pytest`、`pytest-cov`、Coverage、Black 纳入 dev 依赖；新环境可直接执行测试 | — |
| 0.2 架构护栏 | 部分完成 | Extension setup 回滚、冻结 Package 身份拒绝替换、实际 ModelContext/请求快照一致性、Session tree 删除、多 Approval 与 cancel 竞争使用 revision CAS | 后代级联取消随对应实现批次补齐 |
| 1.1 Extension 隔离贡献集 | 完成 | setup 写入 draft，成功后统一发布；失败执行 LIFO disposer；Tool、Hook、Recall、Event Processor、Provider 和 MCP status 不残留半激活状态 | Registry Lease、ExtensionInstance 和 RuntimeGeneration 属于 1.2，不由本批次提前实现 |
| 1.2 RuntimeGeneration reload | 完成 | 精确 Contribution Lease、ExtensionInstance、独立 ToolBus/Registry、原子切换、旧代引用保留与延迟关闭；MCP 删除模块级装载状态 | 跨进程恢复仍按阶段 2 使用冻结 Package 重建，不持久化 `generation_id` |
| 1.3a SQLite v10 最小垂直切片 | 完成 | v10 schema、v9 一次性迁移、双 Store 领域合同、Session/Node、Inbox、Operation/AgentRunState、Package、Artifact、App 调用方切换 | v9 解析仅保留在一次性迁移模块 |
| 1.3b Session 删除语义 | 完成 | 单 Session 与显式子树删除前置条件、双 Store 一致性；旧 Object/Reference 生产模块与测试已删除 | 后代取消属于阶段 6 |
| 2.0 Agent Package v10 | Loader 完成 | 内容寻址 Package ID、三层 ModelPolicy 数据结构、ImplementationRef、SecretRef、Tool replay policy、严格 v10/legacy codec；Provider/Tool 按冻结引用装载；Extension 按模块 version + source digest + 脱敏 config 精确解析 | 当前 Config 仅能构建 primary，worker/utility 的配置来源归入配置升级批次 |
| 2.1–2.3 Operation 恢复核心 | 完成 | active Operation 接受事务、revision CAS、Model/Tool intent-before-effect、safe/never 恢复、Node+State 原子提交、Agent/Driver/App 接线；Package/Secret/实现不可用时持久化为 retryable failed | — |
| 2.4 Host 控制面 | 部分完成 | cancel 持久化入口；`ApprovalService` revision CAS；精确 Package 的 PreToolUse `allow/ask/deny`、更新参数冻结与 schema 校验；`ask → waiting_approval`，拒绝统一为 `rejected` 并按原始顺序生成 ToolResult | `resume_operation` / `reconcile_tool_call` 显式入口 |

第三轮验收基线：`746 passed, 4 skipped`，整体覆盖率 77%，Black 与 `git diff --check` 通过。生产清理统一经过 `ContributionScope.close()`；旧 `AgentRuntime`、`RuntimeBindings`、`RuntimeStore`、`StorageTransaction`、`ImmutableObject` 和 `NamedReference` 生产路径已删除。

## 5. 阶段 0：回归基线

### 0.1 测试入口

- 将 `pytest`、`pytest-cov` 等加入正式开发依赖。
- 修复当前格式检查问题。
- 固定 `uv run pytest`、覆盖率和 Black 检查命令。

验收：新环境执行 `uv sync` 后可以直接运行全部测试，不依赖临时安装。

### 0.2 架构护栏

先添加六组回归测试：

1. Extension setup 失败后不残留 Tool、Hook、Recall 或其他贡献。
2. 父 Session 存在 child Session 或 AgentDelegation 时，各 Store 的删除行为一致。
3. unfinished Operation 只能使用接受时绑定的 AgentPackageVersion 恢复。
4. 可以观测模型实际使用的 ModelContext 和请求快照。
5. 多个 Tool Approval、Approval 与 cancel 并发时，CAS 不丢更新；过期批准不能在重读最新 State 后被盲目重放。
6. Parent Operation 取消后，live、waiting 和重启后未激活的后代 Operation 都会持久化进入 cancelling 并继续安全收敛。

允许测试在对应实现批次前暂时失败，但必须明确关联后续批次，不能用跳过长期隐藏。

## 6. 阶段 1：正确性缺陷

### 1.1 Extension 隔离贡献集

```text
ExtensionInstance
├── contributions
│   ├── tools
│   ├── hooks
│   ├── recall_sources
│   ├── event_processors
│   └── providers
└── disposers[]
```

- setup 期间只修改 Extension 自己的 draft。
- setup 成功后一次性发布贡献。
- setup 失败时反向执行全部 disposer，然后丢弃 draft。
- 一个 disposer 失败不能阻止其余资源清理。

### 1.2 RuntimeGeneration reload

```text
构建新 RuntimeGeneration
→ 完整校验
→ 原子切换
→ 关闭旧 RuntimeGeneration
```

reload 失败时旧 generation 继续服务；旧 Extension Context 在切换后必须明确报告 stale，不能继续修改新 generation。

### 1.3 SQLite v10 最小垂直切片与 Session 删除

审计确认 v9 不具备 `archived_at`、`active_operation_id`、持久化 Inbox、当前 AgentRunState 和目标 Delegation 约束；如果先实现删除，只能继续扫描 `ImmutableObject + NamedReference` 或建立临时双轨。因此实施顺序调整为：

```text
1.3a SQLite v10 最小垂直切片
→ 1.3b Session 删除语义
→ 阶段 2 Operation 恢复主链
```

1.3a 迁移删除与恢复共同依赖的最小闭包：

- `workspaces`；
- `conversation_sessions` / `conversation_nodes`；
- `agent_inbox_messages`；
- `agent_package_versions`；
- `session_operations`；
- `agent_run_states`；
- `artifacts`；
- `agent_delegations`。

Workspace、Package Version 与 Artifact 不是额外产品能力，而是完整 v10 schema 的身份和引用闭包。完整字段、外键、CAS 和接受事务以数据库实体设计与 Operation 恢复合同为准。此批次不是再建一套 v10 Adapter；Store、Service 和调用方切换完成后直接删除 v9 生产读写路径。阶段 5 只负责残余调用方、通用 Persistence 抽象和性能验收，不再进行第二次 schema 改造。

v9 一次性迁移遵循保守规则：旧 `cwd` 规范化后生成 Workspace；旧 archived Session 使用其 `updated_at` 作为 `archived_at` 并记录迁移 warning；Delegation 的初始消息必须能从 child Operation 输入节点唯一推导，否则整次迁移回滚。缺少冻结 ModelContext/Intent、无法判断工具副作用或无法确定 waiting reason 的非终态 Operation 不允许静默重放，转换为稳定迁移错误并进入终态 `failed`；不得降级成 queued/ready。迁移成功后 Runtime 只读写 v10，v9 解析器只存在于一次性迁移模块。

1.3b 的公共删除要求 Session 已归档、无 pending Inbox、无 active Operation 且不存在 Delegation。需要删除整棵关系时使用显式 `delete_session_tree`，并验证目标子树全部归档、空闲且根没有外部 parent Delegation。

验收：SQLite 与测试存储行为一致，不产生悬空 Delegation，不隐式级联删除 child agent 数据。

## 7. 阶段 2：Operation 恢复主链

阶段 2 的依赖顺序固定为：

```text
AgentPackageVersion v10
→ active Operation 发现与装载
→ OperationDriver 恢复决策
→ Host 控制面
```

Package Snapshot 决定 Provider、Tool、Extension 和 Workspace 的恢复身份，因此必须先于 Driver 与 App 调用方完成；否则执行链接入后仍需再次修改 Operation 接受和装载合同。

### 2.1 冻结 Agent Package 装载

恢复必须通过 Operation 保存的 `agent_package_version_id` 装载 AgentPackageVersion，不能使用当前配置代替。快照至少冻结：

- `primary / worker / utility` 模型角色、Provider 和请求设置；
- 逻辑 `SecretRef`，不保存 secret；
- Tool implementation_ref、schema、版本和 replay policy；
- Extension implementation_ref、非敏感配置和 Hook/Recall 版本；
- WorkspacePolicy，不保存 Workspace 的实际绝对路径；
- Behavior 与 Skill 全文。

接受前校验冻结实现和当前 Secret。恢复时精确实现或 Secret 不可用则写入稳定、`retryable=true` 的 AgentRunError 并进入 failed，不静默切换到当前实现，也不增加未被状态机承载的 suspended/blocked 状态。

### 2.2 active Operation 发现

`ConversationSession.active_operation_id` 直接指向当前非终态 AgentRun Operation。打开 Session 时按该指针装载 SessionOperation 和 AgentRunState；指针为空表示没有待恢复执行。接受新 AgentRun 必须以 `active_operation_id IS NULL` 为事务前置条件，终态提交必须在同一事务清空指针。

### 2.3 恢复决策

```text
没有未完成 ToolCall
→ 从 current ModelStep 恢复

ToolCall 已完成
→ 继续状态转换

Tool intent 已提交且 replay_policy=safe
→ 允许自动重试

Tool intent 已提交且 replay_policy=never
→ waiting_reason=tool_reconciliation，等待 reconcile
```

### 2.4 Host 控制面

只接通最小入口：

- `resume_operation`
- `reconcile_tool_call`
- `cancel_operation`

本阶段不同时开发完整交互界面。

## 8. 阶段 3：Context 唯一准备管道

保留两个边界：

```text
ConversationProjector + ContextWindow + RuntimeEffects + ModelContextBuilder
  固定 leaf 与 Package → Provider-neutral ModelContext

Provider Mapper
  ModelContext → Provider wire request
```

固定准备顺序：

```text
固定 leaf 的 Conversation Tree 投影
→ ContextWindow
→ Recall
→ 请求前 Hook 的 ContextContributions
→ ModelContextBuilder（唯一创建入口）
→ 持久化 ModelRequestIntent
→ Provider Mapper
```

要求：

- Runtime 不再手工重复构造 ModelContext。
- Provider 不读取 Session，不自行组装历史。
- Recall 和请求前 Hook 在 Intent 提交后禁止再次执行。
- 未完成请求的 `/context` 读取当前 ModelRequestIntent。
- 模型响应提交后从恢复状态清除完整 ModelContext；历史完整请求只在显式启用 full trace 时保存，默认保留 fingerprint。
- 未执行请求只能生成纯 preview，不执行带副作用的 Recall 或 Hook。
- preview 与 actual 必须明确区分。

## 9. 阶段 4：统一执行身份

收敛为一个值对象：

```python
ExecutionIdentity(
    session_id,
    operation_id=None,
    step_id=None,
    step_sequence=None,
    tool_call_id=None,
    message_id=None,
)
```

迁移顺序：

1. EventEnvelope；
2. Tool execution context；
3. Hook context；
4. Observation records；
5. HostCall context；
6. Streaming events。

`step_sequence` 和 `call_sequence` 只表示顺序，不作为身份。每迁移一个边界即删除对应重复 DTO，不长期保留 adapter。Lane 尚未成为执行边界，因此身份中不包含 `lane_id`。

`TraceMode.full` 增加 Delta TraceSink 验收：记录携带 occurred_at 和 ExecutionIdentity，慢 Sink 不阻塞 Provider stream，队列满时允许丢帧并报告 Diagnostic。该文件不能参与 Operation 恢复或被表述为可靠审计日志。

## 10. 阶段 5：Persistence 收敛

实体决策已确认：AgentRunState 使用一行可 CAS 更新的当前状态，当前 ModelStepState 和 ToolCallState 嵌套其中，不保存历史 State revision。目标直接领域表为：

```text
workspaces
conversation_sessions
conversation_nodes
agent_inbox_messages
session_operations
agent_run_states
agent_package_versions
artifacts
agent_delegations
```

领域事务包括：

- `accept_operation`
- `load_agent_run_state`
- `record_model_intent`
- `record_tool_intent`
- `complete_tool_call`
- `complete_operation`

按顺序实施：

1. 建立 v10 空库 schema 与数据库 CHECK/FK/index；
2. 引入领域化读写方法和 SQLite contract tests；
3. 切换 Workspace、Session、Inbox、SessionOperation 和 AgentRunState；
4. 压平 ConversationNode 内容并切换 Context 投影；
5. 提供一次性、事务化 v9 → v10 migration；
6. 删除 ImmutableObject、NamedReference、StorageCommit 与 ConversationEntry 生产路径；
7. InMemory Store 若保留，只实现与 SQLite 相同的窄领域合同，不复制通用事务模型。

Conversation Tree 保留 `(session_id, parent_node_id)` 索引；对 1,000/10,000 Node 长分支执行查询计划与投影基准。没有性能证据时不增加 Closure Table、materialized path、Branch Entity 或路径缓存。

如果未来需要可靠事件发布，单独增加窄用途 outbox，不恢复通用 Commit 抽象。

## 11. 阶段 6：Agent Delegation 最小闭环

不同 Agent 使用独立 child Session：

```text
Parent Session
├── AgentDelegation → Child Session A → Child Operation A
└── AgentDelegation → Child Session B → Child Operation B
```

child Session 使用自己的 AgentPackageVersion 和 `active_node_id`。父子执行关系只由 AgentDelegation 保存，不在 ConversationSession 中重复保存 `parent_session_id` 或 `forked_from_node_id`。

接口拆分为：

- `start_delegation(...) -> child_session_id`
- `wait_delegation(...)`
- `cancel_delegation(...)`

`start_delegation` 不同步等待 child 完成。父 Agent 通过 AgentDelegation 和 Artifact 获得结果；child Session 的 `active_operation_id` 和对应 AgentRunState 是执行状态权威。

Parent Operation 进入 `cancelling` 后，必须沿 AgentDelegation 图查询全部非终态后代，以幂等 CAS 写入 child cancellation，再按 child Session 调用 `AgentRegistry.wake()`。不能只遍历 live Agent；父取消恢复时必须重做级联检查。child 由自己的 OperationDriver 协调 Tool Intent，Parent 达到安全边界后才能终态。

验收：多个 child 可以并行；单个 child 失败不污染其他 Session；进程重启后可以分别恢复；child 使用自己的冻结 Package；父取消不会遗留运行或 waiting 的孤儿 Operation，也不会删除无关用户 InboxMessage。

## 12. 阶段 7：清理与最终验收

逐项处理只有类型、没有产品路径的能力：

| 能力 | 处理原则 |
| --- | --- |
| RequestDigestEvent | 实际请求快照接通后删除重复事件 |
| HostCallRecorder | 接通真实调用，否则移出公共 API |
| ActivationControl | 接入 RuntimeHost 启动路径，否则删除 |
| AgentRunUsage | Provider 能提供则接通，否则暂不公开 |
| `/model`、`/thinking` | 实现 Package 切换语义，否则隐藏 |
| Gemini Boot | 完成生产路径或明确标记未支持 |
| 重复 ArtifactService 注入 | 保留唯一依赖来源 |

最终校对本文引用的领域合同，更新原文，不创建同主题 v2/v3。

## 13. 每个批次的完成标准

一个批次必须同时满足：

1. 新行为有自动化测试。
2. 相关测试和全量测试通过。
3. Black 和项目约定的静态检查通过。
4. 没有新增未使用的公共类型和预留接口。
5. 没有旧、新两条生产路径长期并存。
6. 没有顺手引入下一阶段抽象。
7. 对应领域合同与实现一致；存在冲突则停止合并。
8. 提交只说明一个可独立理解的变化，并可以完整 revert。

## 14. 第一轮实施范围

第一轮只实施：

1. 0.1 测试入口；
2. 0.2 六组架构护栏；
3. 1.1 Extension 隔离贡献集。

第一轮不修改数据库结构、不修改 Operation 状态模型、不引入 Lane。完成后复盘批次大小和回归成本，再决定是否进入 RuntimeGeneration reload。

## 15. 已锁定的实施前置

以下内容不再作为实现中的自由选择：

1. Persistence 使用 SQLite v10 明确领域表；AgentRunState 是一行当前状态，不保存历史 revision。
2. 一个 ConversationSession 同时最多一个非终态 AgentRun，以 `active_operation_id` 和接受事务保证。
3. Extension 贡献按 RuntimeGeneration 构建，ContributionScope 统一 LIFO rollback/close。
4. Session 使用 `archived_at`；公共 delete 默认拒绝关系图，显式 delete_session_tree 删除完整 child 子树。
5. Tool 副作用未知由 `intent_recorded + waiting_reason=tool_reconciliation` 表达，不增加 outcome_unknown status。
6. Root/Child 使用统一 Agent 和独立 Session，不引入 Lane。
7. Approval 使用 AgentRunState revision CAS；不增加正确性锁或盲目重试。
8. 新需求若要求改变上述任一项，先更新命中的领域合同，再修改代码。
