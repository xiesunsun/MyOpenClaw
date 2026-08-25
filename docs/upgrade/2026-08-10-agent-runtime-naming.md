# Agent Runtime 重构命名约束

**日期**：2026-08-10
**更新日期**：2026-08-25
**状态**：目标合同，实施中
**范围**：Agent Runtime、持久化实体、执行状态、Context、多模态、多 Agent 与生命周期组件的唯一名称
**不在范围**：数据库列级定义、Provider wire 协议和实施排期

本文只回答“一个概念叫什么、负责什么”。数据库字段遵循 [`数据库实体设计`](./2026-07-12-db-entities.md)，执行恢复遵循 [`Operation 持久化与恢复模型`](./2026-08-11-operation-recovery-model.md)，详细理由遵循 [`Runtime 实体决策`](./2026-08-24-runtime-entity-decisions.md)。旧实现出现同义名称时，以本文为目标态。

## 1. 命名原则

1. 一个概念只有一个名称，一个名称只表达一种生命周期。
2. Entity 表示稳定身份；Value Object 表示无独立生命周期的数据；Service/Driver 只承担窄行为。
3. `State` 只表示可持久化、可恢复的当前执行状态。
4. `Definition` 是可编辑来源，`Version` 是不可修改且可稳定引用的冻结内容。
5. `Intent` 表示外部副作用发生前已经提交的精确决定。
6. `Event` 是可丢失通知，不是恢复事实；`Trace` 是可丢失诊断副本。
7. 禁止用 `Manager`、`Coordinator`、`Processor`、`Handler`、`Context` 或任意资源袋掩盖多种职责。
8. 迁移完成后删除旧名，不保留永久 Alias、Adapter 或双轨生产路径。

固定后缀：

| 后缀 | 唯一语义 |
| --- | --- |
| `State` | 可更新、可恢复的当前状态 |
| `Intent` | 跨越外部执行边界前冻结的决定 |
| `Event` | 已发生事实或生命周期的进程内通知 |
| `Snapshot` | 明确边界捕获的完整只读诊断视图 |
| `Definition` | 用户可编辑的源定义 |
| `Version` | 内容冻结、创建后不修改的版本 |
| `Reference` | 对另一稳定 Entity 的值引用；不表示可移动数据库指针 |
| `Operation` | 已被 Session 接受、可恢复且具有明确终态的工作 |
| `Step` | AgentRun 中一次模型请求、响应和工具批次 |
| `Driver` | 重复推进一个明确状态机直到等待或终态 |

## 2. 执行层级

```text
ConversationSession
└── SessionOperation                 不可变执行身份
    └── AgentRunState                当前唯一执行状态
        └── ModelStepState?          当前模型步骤
            └── ToolCallState[]      当前工具调用状态
```

| 名称 | 唯一含义 | 是否独立落库 |
| --- | --- | ---: |
| `ConversationSession` | 一棵 Conversation Tree、一个活动位置和 Inbox 归属 | 是 |
| `SessionOperation` | Session 接受的一次不可变 AgentRun 身份与执行环境绑定 | 是 |
| `AgentRunState` | 一个 Operation 的当前可恢复状态 | 是，一行 CAS 更新 |
| `ModelStepState` | 当前模型请求、响应及工具批次 | 否，嵌入 AgentRunState |
| `ToolCallState` | 当前 ToolCall 的审批、Intent、执行与结果状态 | 否，嵌入 ModelStepState |

当前只有一种 SessionOperation：AgentRun。因此不保存 `operation_type`，也不创建独立 `AgentRun` 表。只有第二种工作同时需要 Session 接受、持久化、恢复、resume/cancel 和明确终态时，才给 SessionOperation 增加类型判别。

`Turn`、`Job`、`Task` 不作为 Runtime 核心实体。统一身份：

```text
session_id
operation_id
step_id
tool_call_id
message_id
```

不再为同一执行维护 `turn_id`、`run_id`、`batch_id` 或 `lane_id`。

## 3. Runtime 组件

```mermaid
flowchart LR
    H[RuntimeHost] --> G[RuntimeGeneration]
    H --> R[AgentRegistry]
    R --> A[Agent]
    A --> I[AgentInbox]
    A --> D[AgentDriver]
    D --> O[OperationDriver]
    O --> S[AgentRunStateMachine]
    O --> E[RuntimeEffects]
```

| 名称 | 唯一职责 |
| --- | --- |
| `RuntimeHost` | 进程启动、shutdown、配置 reload 和 RuntimeGeneration 切换 |
| `RuntimeGeneration` | 一代完整可执行贡献及其生命周期所有权 |
| `ContributionScope` | 注册贡献和外部资源的 LIFO 撤销边界 |
| `AgentRegistry` | 单进程 `session_id → live Agent` 唯一映射、引用与唤醒 |
| `AgentHandle` | 一个调用方对 live Agent 的精确、幂等引用 |
| `Agent` | Root/Child 平等的消息、取消和等待接口 |
| `AgentInbox` | 持久化 InboxMessage 的内存窄投影，不保存第二份队列 |
| `AgentDriver` | 判断 Session 是否 runnable，串行接受或恢复 Operation |
| `OperationDriver` | 推进一个已有 Operation，直到 waiting 或终态 |
| `AgentRunStateMachine` | 校验 AgentRunState、ModelStepState 和 ToolCallState 转换 |
| `RuntimeEffects` | Provider、Tool、Hook、Recall、Timer 等外部作用的窄执行边界 |

删除目标态中的 `AgentRuntime` 和 `RuntimeBindings`。前者的接受/调度职责分别进入 AgentDriver 与 OperationDriver；后者的 Package 实现进入 LoadedAgentPackage，Host 级服务显式传给 RuntimeEffects。

`ConversationRuntime` 只允许作为 Host/UI Adapter 临时存在；它不拥有业务状态机、Context、Provider 或 Tool Loop。迁移完成后若无独立产品职责则删除。

## 4. Agent Package

| 名称 | 唯一职责 |
| --- | --- |
| `AgentDefinition` | 从 Pickel 配置、AGENT.md 等来源解析出的可编辑蓝图 |
| `AgentPackageVersion` | 内容寻址、不可修改的一次执行配置快照 |
| `LoadedAgentPackage` | 当前 RuntimeGeneration 中解析了 Secret 与可执行实现的 Package |
| `LoadedPackageHandle` | Operation 对 LoadedAgentPackage 和 Generation 的引用 |
| `ModelPolicy` | `primary / worker / utility` 三层模型选择 |
| `AgentRuntimePolicy` | 最大 Step、Context Window、Delegation 深度等执行限制 |
| `WorkspacePolicy` | Package 声明的文件访问范围 |
| `WorkspaceBinding` | Operation 接受时冻结的实际执行目录和安全边界 |

每个 SessionOperation 接受时绑定确定的 `AgentPackageVersion` 和 `WorkspaceBinding`。Definition、Settings 或 Environ 的后续变化只影响未来 Operation。

## 5. 持久化实体

| 名称 | 身份 | 职责 |
| --- | --- | --- |
| `Workspace` | `workspace_id` | 实际目录的长期身份 |
| `ConversationSession` | `session_id` | 会话树、活动位置、active Operation 和归档状态 |
| `ConversationNode` | `node_id` | 树位置与 Provider-neutral 类型化内容 |
| `InboxMessage` | `message_id` | 持久化输入、FIFO、delivery 和 claim 结果 |
| `SessionOperation` | `operation_id` | 不可变执行身份、Package 与 Workspace 绑定 |
| `AgentRunState` | `operation_id` | 当前可恢复执行状态 |
| `AgentPackageVersion` | `package_version_id` | 内容寻址配置快照 |
| `Artifact` | `artifact_id` | 内容寻址二进制元数据 |
| `AgentDelegation` | `child_session_id` | Parent Operation 与长期 child Session 的因果关系 |

ConversationNode 直接保存 `agent_message` 或 `history_compaction` 内容。目标态删除：

- `ImmutableObject`
- `NamedReference`
- `StorageCommit`
- `ConversationEntry`
- `ConversationNode → Object` 间接层
- Operation State 历史 Snapshot 链

`ConversationSession.active_node_id` 是活动位置的唯一权威；`active_operation_id` 是恢复当前 Operation 的唯一入口；`AgentRunState.revision` 是执行状态 CAS 权威。

## 6. Context

```text
ConversationProjector
    Conversation Tree → Conversation Messages

ContextWindow
    Conversation Messages → Visible Messages

RuntimeEffects
    Visible Messages → Recall/Hook ContextContributions

ModelContextBuilder
    Package + Visible Messages + Contributions → ModelContext

Provider Request Mapper
    ModelContext → Provider wire request
```

| 名称 | 唯一职责 |
| --- | --- |
| `ConversationProjector` | 沿固定 leaf 投影 AgentMessage 和 HistoryCompaction |
| `ContextWindow` | 只裁剪 Conversation Messages |
| `ContextContributions` | Recall/Hook 返回的深度不可变追加数据 |
| `ModelContextBuilder` | 创建唯一 Provider-neutral ModelContext |
| `ModelContext` | 深度不可变的 system/messages/tools |
| `ModelRequestIntent` | 当前 Step 已决定发送的完整 ModelContext 和 fingerprint |
| `AnthropicRequestMapper` | ModelContext 到 Anthropic wire 的纯映射 |

删除 `ContextAssembler`、`ContextPipeline`、`ContextManager`、`PreparedContext` 和 Provider 周围的二次组装。`prepare()` 统一为 `build_model_context()`；generate、stream、count_tokens 和 RequestSnapshot 复用同一 Provider Mapper。

`Provider.request_snapshot(ModelContext)` 只返回实际 generate/stream 使用的 wire
request，不混入 provider、model 或 cache order 包装。RuntimeEffects 使用 Provider
声明的 `request_cache_order` 组装 RequestSnapshotRecord；不得靠字典字段猜测快照
类型。Hook 不得返回另一份完整 ModelContext 覆盖 Builder 结果，只能在 Intent
提交前提供受限 ContextContributions。

`/context` 是只读视图：当前 Step 有 ModelRequestIntent 时直接展示其中已提交的
精确 ModelContext，source 为 `model_request_intent`；没有 Intent 时才执行不含
Recall/Hook/draft input 的纯 preview，source 为 `preview`。preview 不能伪装成
实际请求，也不能为了检查而触发外部副作用。

## 7. Tool、Approval 与 HostCall

| 名称 | 语义 |
| --- | --- |
| `ToolCallState` | 当前 ToolCall 的持久化状态 |
| `ToolExecutionIntent` | Tool 外部副作用前冻结的工具特定决定 |
| `ToolReplayPolicy` | `safe / never` 自动重放策略 |
| `ToolApproval` | 嵌入 ToolCallState 的持久化审批请求和决定 |
| `ApprovalService` | 通过 revision CAS 接受批准或拒绝 |
| `ToolReconciliationService` | 通过 revision CAS 接受 Host 对已提交 Tool Intent 的核对结果 |
| `HostCallSpec` | 瞬时、类型化 Host 能力定义 |
| `HostCallRouter` | 进程内 Host 请求—响应路由，不负责恢复 |

`HostCall` 不等于可恢复外部等待。需要重启后继续等待的交互必须由所属业务状态机持久化；当前 ToolApproval 和 Tool reconciliation 都直接修改 AgentRunState，不新增独立队列或 Manager。

## 8. 多模态与多 Agent

消息内容使用明确 Block：

```text
ContentBlock
├── TextBlock
├── ArtifactBlock
├── ThinkingBlock
└── ToolCallBlock
```

`ArtifactReference` 是消息内的值对象；`Artifact` 是全局持久化元数据；`BlobStore` 保存实际字节。

Root 与 Child 都是同一个 `Agent` 类型。Child 使用独立 ConversationSession，通过 AgentDelegation 连接 Parent Operation；不引入 `RootAgent`、`ChildAgent`、`DelegatedAgentRun`、`SessionLane` 或 Agent Team 层级。

推荐方法名：

```python
Agent.followup()
Agent.steer()
Agent.inject()
Agent.cancel()
Agent.resume_operation(operation_id)
Agent.when_idle()

start_delegation() -> child_session_id
wait_delegation()
cancel_delegation()
```

## 9. Observation 与生命周期

| 名称 | 职责 |
| --- | --- |
| `ExecutionIdentity` | session/operation/step/tool/message 的统一引用 |
| `RuntimeEvent` | fact/lifecycle/delta 的进程内 tagged union |
| `EventEnvelope` | event_id、identity、时间和单 stream 顺序 |
| `SpanRecord` | 一次调用或阶段的测量层级 |
| `DiagnosticRecord` | 结构化诊断 |
| `RequestSnapshotRecord` | 已提交 ModelContext 映射出的 Provider 请求快照 |
| `TraceSink` | 可丢失诊断副本输出，不是恢复或审计权威 |

## 10. 动词合同

| 动词 | 固定语义 |
| --- | --- |
| `create` | 创建有稳定身份的 Entity |
| `build` | 从确定输入纯组装 Value Object |
| `resolve` | 按规则选择并校验配置或实现 |
| `load` | 按身份读取一个持久化 Entity |
| `find` | 按条件读取零或一个结果 |
| `list` | 返回有确定顺序的集合 |
| `insert` | 写入不可变 Entity |
| `append` | 向有序结构增加内容 |
| `accept` | Session 从 Inbox 原子创建 Operation |
| `claim` | 原子消费 pending InboxMessage |
| `commit` | 原子提交状态转换和关联事实 |
| `project` | 从持久化事实派生只读值 |
| `execute` | 发生真实计算或外部调用 |
| `drive` | 推进状态机直到等待或终态 |
| `wake` | 通知 AgentDriver 重新检查数据库 runnable work |
| `close` | 释放内存资源或引用，不改变业务 Session |
| `archive` | 将 Session 变为持久化只读状态 |

避免单独使用 `save/update/write/set/process/handle`；方法名必须表达目标和语义，例如 `commit_agent_run_state()`、`claim_step_messages()`。

## 11. 历史名称迁移

| 历史名称 | 目标处理 |
| --- | --- |
| `Run` | 删除；拆入 RuntimeHost、Agent、Driver 和 Effects |
| `AgentRuntime` | 删除；接受/调度进入 AgentDriver，推进进入 OperationDriver |
| `RuntimeBindings` | 删除；Package 实现进入 LoadedAgentPackage，Host 服务显式注入 |
| `ExecutionStrategy` / `ReActStrategy` | 删除；默认 Tool Loop 属于 OperationDriver |
| `ContextAssembler` / `prepare()` | 删除；使用 ModelContextBuilder.build_model_context() |
| `TurnState` / `StepState` | `AgentRunState` / `ModelStepState` |
| `SessionEntry` / `ConversationEntry` | 删除；ConversationNode 直接保存类型化内容 |
| `leaf_id` / NamedReference active | `ConversationSession.active_node_id` |
| `current_commit_sequence` | 删除；按领域使用自然 CAS 或 AgentRunState.revision |
| `execution_policy` | 删除；ToolCallStatus + ToolApproval 表达 |
| `pending_context_feedback` | 删除；使用 InboxMessage.inject |

## 12. 验收约束

1. 代码和当前合同中不存在同义的 Run/AgentRuntime/Strategy 执行入口。
2. 同一 Session 在单进程只有一个 live Agent 和一个 AgentDriver task。
3. OperationDriver 不组装 Context，不持有 Host/UI 状态，不成为依赖资源袋。
4. ModelContext 只有一个 Builder，Provider 只映射 wire。
5. ConversationNode 不保存运行状态，AgentRunState 不保存历史消息内容。
6. 当前 Operation 通过 Session.active_operation_id 恢复，不扫描历史 Operation。
7. Tool 外部副作用前必须提交 ToolExecutionIntent；未知结果不得静默重放。
8. RuntimeGeneration reload 后旧 Operation 继续引用原 LoadedAgentPackage，所有贡献可逆序撤销。
9. Root/Child 共用 Agent、Inbox、Operation 和 Driver，不引入 Lane。
10. 完成迁移的旧公共类型、表和兼容路径必须删除。
