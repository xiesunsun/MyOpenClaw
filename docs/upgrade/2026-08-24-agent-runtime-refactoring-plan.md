# Agent Runtime 重构实施计划

**日期**：2026-08-24  
**更新日期**：2026-08-27
**状态**：阶段 0–10 完成
**范围**：Runtime、Context、Operation 恢复、执行身份、持久化、Extension 生命周期、Agent Delegation 与 Provider 调用可靠记录的分阶段重构
**不在范围**：Lane、通用事件溯源、Workspace 聚合根、完整插件框架、新界面功能

本文只规定实施顺序、批次边界和验收门槛，不重复定义领域实体。具体名称和语义遵循：

- [`Agent Runtime 重构命名约束`](./2026-08-10-agent-runtime-naming.md)
- [`数据库实体设计`](./2026-07-12-db-entities.md)
- [`配置系统升级设计`](./2026-07-25-config-system-design.md)
- [`Operation 持久化与恢复模型`](./2026-08-11-operation-recovery-model.md)
- [`Anthropic Provider 设计`](../superpowers/specs/2026-04-21-anthropic-provider-design.md)

逐项确认的实体边界记录在 [`Runtime 实体决策`](./2026-08-24-runtime-entity-decisions.md)。

领域合同已经统一到 Runtime 实体决策：当前实现为 SQLite v12 明确领域表，v11 是 `model_calls` 迁移来源，v10 是增加 child Package 稳定绑定的历史迁移来源。当前状态继续使用 revision CAS。`ImmutableObject + NamedReference + StorageCommit` 只作为 v9 迁移来源。

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
| 8 | 收敛 Tool 输出与 Delegation 控制契约 | Tool 输出单一模型可见内容；child 终态结果、report 和中断语义无歧义 |
| 9 | 收敛 Multi-Agent 消息与 Package 选择 | child 终态主动投递；模型不依赖固定顺序等待；child Agent 选择可冻结、可恢复且不扩权 |
| 10 | 建立 ModelCall 可靠数据底座 | 已完成：请求前保存、响应聚合、调用身份与恢复边界均已接通 |

### 4.1 当前进度（2026-08-27）

| 批次 | 状态 | 已完成 | 尚未进入 |
| --- | --- | --- | --- |
| 0.1 测试入口 | 完成 | `pytest`、`pytest-cov`、Coverage、Black 纳入 dev 依赖；新环境可直接执行测试 | — |
| 0.2 架构护栏 | 完成 | Extension setup 回滚、冻结 Package 身份拒绝替换、实际 ModelContext/请求快照一致性、Session tree 删除、多 Approval 与 cancel 竞争使用 revision CAS、Parent Operation 后代级联取消 | — |
| 1.1 Extension 隔离贡献集 | 完成 | setup 写入 draft，成功后统一发布；失败执行 LIFO disposer；Tool、Hook、Recall、Event Processor、Provider 和 MCP status 不残留半激活状态 | Registry Lease、ExtensionInstance 和 RuntimeGeneration 属于 1.2，不由本批次提前实现 |
| 1.2 RuntimeGeneration reload | 完成 | 精确 Contribution Lease、ExtensionInstance、独立 ToolBus/Registry、原子切换、旧代引用保留与延迟关闭；MCP 删除模块级装载状态 | 跨进程恢复仍按阶段 2 使用冻结 Package 重建，不持久化 `generation_id` |
| 1.3a SQLite v10 最小垂直切片 | 完成 | v10 schema、v9 一次性迁移、双 Store 领域合同、Session/Node、Inbox、Operation/AgentRunState、Package、Artifact、App 调用方切换 | v9 解析仅保留在一次性迁移模块 |
| 1.3b Session 删除语义 | 完成 | 单 Session 与显式子树删除前置条件、双 Store 一致性；旧 Object/Reference 生产模块与测试已删除 | 后代取消属于阶段 6 |
| 2.0 Agent Package v10 | 完成 | 内容寻址 Package ID、三层 ModelPolicy 数据结构与 `models.primary/worker/utility` 配置来源、ImplementationRef、SecretRef、Tool replay policy、严格 v10/legacy codec；三个模型角色按冻结 wire 实现装载；Extension 按模块 version + source digest + 脱敏 config 精确解析 | 具体产品功能按需显式选择角色，不做隐式 fallback |
| 2.1–2.3 Operation 恢复核心 | 完成 | active Operation 接受事务、revision CAS、Model/Tool intent-before-effect、safe/never 恢复、Node+State 原子提交、Agent/Driver/App 接线；Package/Secret/实现不可用时持久化为 retryable failed | — |
| 2.4 Host 控制面 | 完成 | `Agent.resume_operation(operation_id)` 精确身份恢复；cancel 持久化入口；`ApprovalService` revision CAS；精确 Package 的 PreToolUse `allow/ask/deny`；`ToolReconciliationService` 对 `completed/not_started/unknown` 做单次 revision CAS，取消中的已完成结果经可恢复的两步 CAS 收敛 | 完整交互界面和 AgentRegistry 调度不在本批次 |
| 3.0 Context 唯一管道 | 完成 | 实际请求只有 `ModelContextBuilder` 一个创建入口；Anthropic 生产路径和 Gemini Provider 直接调用测试分别复用各自唯一的 request builder，后者不表示 Boot 已支持 Gemini；`/context` 区分已提交 Intent 与纯 preview；请求前 Hook 只能按注册顺序追加 `ContextContributions`，最终 Context 在 Provider 前冻结进 Intent；旧 `HookFeedback` 与完整 Context 覆盖路径已删除 | — |
| 4.1 EventEnvelope 执行身份 | 完成 | 新增唯一 `ExecutionIdentity` 值对象；`EventEnvelope` 改为组合该身份，不再复制执行字段；Runtime Event、EventBus、Trace、CLI 与 Extension 调用方已迁移，扁平 JSON 增加 `tool_call_id/message_id` | Tool、Hook、Observation、HostCall 与 Streaming 边界按后续小批次迁移 |
| 4.2 Tool execution 执行身份 | 完成 | `ToolExecutionContext` 只组合 `ExecutionIdentity`，删除五个重复执行字段；Boot 在工具执行边界一次性组装完整身份，Shell 与 MCP Proxy 调用方已迁移；`tool_call_id` 继续作为默认幂等身份 | Hook、Observation、HostCall 与 Streaming 边界按后续小批次迁移 |
| 4.3 Hook 执行身份 | 完成 | Hook 控制事件只组合 `ExecutionIdentity`；Pre/Post Tool Hook 的 `tool_call_id` 收敛到统一身份；Lifecycle 与 OperationDriver 调用方已迁移；仅为旧 Hook 身份存在的 `EventIdentity` 已删除 | Observation、HostCall 与 Streaming 边界按后续小批次迁移 |
| 4.4 Observation 执行身份 | 完成 | Span、Diagnostic 与 Request Snapshot 直接组合 `ExecutionIdentity`，Hook/Event/Model Request 调用方不再转换身份；旧 `ObservationIdentity` 已删除；观测时间字段与既有 Trace JSON schema 保持不变 | HostCall 与 Streaming 边界按后续小批次迁移 |
| 4.5 HostCall 执行身份 | 完成 | `HostCallContext` 只保留独立 `call_id`、统一 `ExecutionIdentity` 与 timeout；MCP Proxy 和嵌套 elicitation 直接复用 ToolCall 身份；Router 的 pending/cancel/dedup 仍以每次 Host 请求的 `call_id` 为权威 | Streaming 边界按后续小批次迁移 |
| 4.6a Streaming 执行身份 | 完成 | Provider-neutral `StreamDelta` 保持无 Runtime 身份；RuntimeEffects 在已校验 Operation/Step 边界为每个 Delta 附加 `ExecutionIdentity`；ConversationRuntime 不再猜测身份；ToolCall Args Event 的重复 `tool_call_id` payload 已删除 | 4.6b Full Trace 丢帧 Diagnostic 验收 |
| 4.6b Full Trace 丢帧 Diagnostic | 完成 | Full Trace 使用有界队列异步写入；每次连续 Delta 拥塞只在队列外报告首个 `trace_delta_dropped` Diagnostic，保留真实执行身份；慢回调不阻塞 Provider，flush/close 会排空单槽 pending diagnostic，且不参与恢复或可靠审计 | — |
| 5.0 Persistence 收敛 | 完成 | SQLite/InMemory 统一为 v10 窄领域合同；Operation/State 只能通过原子 `accept_operation` 创建，`commit_run_transition` 是唯一 State CAS 提交入口；旧通用生产路径已删除；递归 CTE 沿 `parent_node_id → node_id` 主键回溯，1,000/10,000 Node 与 EXPLAIN 查询计划通过 | — |
| 6.1 AgentRegistry | 完成 | `session_id → live Agent` 唯一映射；同 Session 普通 reopen 复用同一 Host/UI Adapter；幂等 wake 不丢运行期追加唤醒；Agent 锁串行前台与后台 drive；detach 按 expected Agent 注销；reload 仅在新代 attach 成功后替换 Agent，失败保留旧代 | active Operation 的 steer/inject 原子 claim 属于 6.2 |
| 6.2a Step 消息持久化合同 | 完成 | SQLite/InMemory 以 `claim_step_messages` 一次提交 steer/inject claim、FIFO User Node 链、Session active node 与 State revision；请求 Intent 冻结和 succeeded 提交受 pending steer/inject 事务护栏约束；OperationService 不绕过状态机 | Driver stopping check 与 idle inject 过滤属于 6.2b |
| 6.2b Step 消息执行路径 | 完成 | idle inject 保持 pending；followup/steer 触发 wake；steer/inject 仅在 preparing_request 安全边界 claim；Intent 冻结后、waiting 与 cancelling 不改当前 Step；无工具 Assistant Node 先持久化，再由事务护栏决定继续当前 Operation 或 succeeded；空闲 cancel 不触发 wake | — |
| 6.3a Delegation durable acceptance | 完成 | `DelegationService` 只从父 `DelegateAgentIntent` 读取冻结 child Package；校验当前 ToolCall、WorkspaceBinding 与 delegation depth；SQLite/InMemory 一次事务创建空 child Session、初始 pending followup 和 AgentDelegation，支持并发幂等且不创建 child Operation/Node | 普通 child Agent 装配、启动恢复和父 ToolCall 结果属于后续批次 |
| 6.3b-1 Headless Agent 激活 | 完成 | active child 使用自身 Operation Package；idle delegated child 使用 parent Operation 的不可变 Package 绑定，不依赖终态后清空的 Step；完整构造后才 register/wake | — |
| 6.3b-2 启动恢复发现 | 完成 | RuntimeHost.create 通过共享 Store 的无分页 `list_runnable_session_ids()` 发现未归档且可运行的普通/child Session；只恢复 queued/running/cancelling 或 idle followup/steer，隔离单候选失败；active Operation 的精确 Package/Tool/Secret 不可装载时以一次 revision CAS 收敛为 retryable failed 并清空 Session 指针，不用当前同名实现替换；delegated idle child 严格拒绝替换冻结 Package | delegation Tool adapter 与父 ToolCall 结果仍属后续批次 |
| 6.3c-a `delegate_agent` durable start | 完成 | Tool 只返回原子接受后的 child Session/message handle；ready→intent_recorded 先冻结父 Operation Package，再调用窄 DelegationControl；ToolSpec 显式传递 replay policy，内置 Tool 使用 `safe`，激活失败只记录日志并由启动恢复兜底 | wait/result/cancel 不在本批实现 |
| 6.3c-b `send_message` direct-child followup | 完成 | Tool 以 `(sender Operation, Step, ToolCall)` 派生稳定 message ID；Tool Result 明确只表示 pending 已持久化，不把 acceptance 伪装成已处理 | — |
| 6.3c-c `list_agents` child snapshot | 完成 | 立即读取 direct child；补充 phase、request attempt、pending message count 和 updated_at，`ready` 明确表示 pending Inbox | — |
| 6.3c-f `wait_delegation` | 完成 | 有界等待 direct child 终态；超时不取消 child，终态从 `final_assistant_node_id` 读取持久化 AssistantMessage | 不新增 Result 实体 |
| 6.3c-d `report` child-to-parent report | 完成 | Tool 只接收自包含 `output`；SQLite/InMemory 以 child Session 的 AgentDelegation 推导唯一 direct parent，原子写入 steer Inbox，稳定 ID 支持已处理/归档后的幂等重放；Host durable append 后 activate/wake parent，失败由启动恢复兜底 | 不绑定 child 终态，不实现 result/settlement/wait |
| 6.3c-e direct-child interruption（旧路径） | 已迁移 | 新 Package 不再公开 `cancel_delegation`；冻结旧 Package 仍可通过隐藏兼容入口恢复执行 | 新生产合同见 8.3 |
| 6.3d Parent Operation 后代级联取消 | 完成 | OperationService 在 parent CAS 进入 `cancelling` 后沿 AgentDelegation 图幂等 CAS 所有非终态后代；双 Store 在 `cancelling → cancelled` 事务内检查后代终态及祖先来源 pending child 消息；只 discard 真实后代的 AgentMessage，child 收敛后唤醒 direct parent，重启继续 reconciliation | 不新增取消实体、Manager 或通用图缓存 |
| 6.4 真实并发闭环 | 完成 | Provider 有界重试、异常终态收敛、跨 Operation FIFO drain、Delegation 重激活、`wait_delegation`、headless Trace、同 Package 并发门槛与状态诊断均已实现 | 真实外部 503 仍可发生，但不再留下假 `running` |
| 8.1 Tool 输出合同 | 完成 | `output_schema` 在 Tool、Package 与 ModelContext 中必填；`execute` 只返回 JSONValue，Runtime 验证后调用纯 `render` 并直接生成唯一 ToolResultMessage；Provider 只投影 content | 历史 v2/v3 payload 的旧字段只读后丢弃，不恢复为结果权威 |
| 8.2 Child 终态结果与 report 分离 | 完成 | child 成功终态只通过 `final_assistant_node_id` 指向最终 AssistantMessage；`report` 仅对 delegated Session 可见，只写 parent 中间通信；`wait_delegation` 只读持久化终态结果 | 成功/失败/取消、report、wait 与恢复路径已有合同测试 |
| 8.3 `interrupt_agent` 控制语义 | 完成 | 新 Package 只公开 `interrupt_agent`；只选择 direct child 当前 active Operation，保留 Inbox、Session 与 AgentDelegation，并复用普通 cancellation 及后代级联；旧名仅隐藏加载冻结 Package | direct-child 权限、非后代拒绝、CAS、级联、恢复与关系保留由 Store/Operation/Package 合同测试覆盖 |
| 9.1 Delegation 结果投影 | 完成 | settled 与兼容 wait 复用紧凑投影；只保留 Text/Artifact 结果并实施长度预算，去除 Thinking 与 Provider metadata | — |
| 9.2 Child 终态主动投递 | 完成 | 双 Store 在 child 终态事务内原子写 direct parent steer；提交后由 Host 去重 wake，Parent 按 Inbox sequence 消费 | — |
| 9.3 模型工具面收敛 | 完成 | Package v3 与默认 ToolBus 不再公开 `wait_delegation`；格式 1/2 冻结 Package 通过隔离兼容包装恢复 | — |
| 9.4 Agent Package v3 | 完成 | 冻结 `AgentDelegationPolicy`、结果预算以及 `delegate_agent.agent_id` 枚举；缺省策略为仅允许自身 | — |
| 9.5 SQLite v11 与跨 Package child | 完成 | Delegation 持久化 `child_package_version_id`；显式 v10→v11 迁移；child 按冻结 Agent Package 装载，Tool 权限沿完整祖先链取交集，Workspace 不扩权 | — |
| 9.6 Multi-Agent 端到端验收 | 完成 | 并行 child 按实际完成顺序投递、重启恢复、report/send 方向、跨 Agent Package 和多层权限均有合同测试；Provider live smoke 通过 | — |
| 10.1–10.4 ModelCall 可靠数据底座 | 完成 | SQLite v12、内容寻址存储、三种 Provider Mapper、发送 Gate、流式聚合、恢复与失败收敛已接通；utility/worker 基础链有双 Store 全链合同测试 | title/history-compaction 产品功能尚未启用，不属于本阶段新增范围 |
| 11.2 观测时序 Span | 完成 | context、semaphore wait、ModelCall 内容写入与 prepare/complete 事务、tool execute、event delivery 均在真实边界发射窄 Span；Trace projector 暴露对应聚合 | Provider generation/first output 继续读取 ModelCall 时间事实；连接内部不可测边界不造零值 |
| 11.7 Provider timeout/retry 内部治理 | 完成 | 共同错误分类、默认 3 次 attempt、1s/2s 指数退避（上限 4s）及首个输出后禁止重试；attempt 复用单一 ModelRequestIntent 并独立持久化 ModelCall | connect/首字节/流空闲由三种 wire 的底层 HTTP/SDK timeout 负责；无独立 total deadline 事实 |

当前验收基线：全量测试为 `1070 passed, 4 skipped`；Ruff、Black 与 `git diff --check` 通过。阶段 4 六个边界已经统一使用 `ExecutionIdentity`，阶段 5 Persistence 已收敛，阶段 6.1 AgentRegistry、6.2 Step 消息消费与阶段 6.3 Agent Delegation 最小闭环已接通。阶段 7 已删除无生产发射路径的 `RequestDigestEvent`、`AgentRunProgress`、`ModelStepStarted` 及其专属 CLI/Trace 残影，移出无真实观测路径的 `HostCallRecorder`，删除未接入 RuntimeHost/Boot 的激活控制，隐藏尚未实现完整切换语义的 `/model`、`/thinking`，明确 Gemini Boot 未支持，删除无调用方的 Provider factory，并统一 ArtifactService 生命周期。交互式 CLI 按 Tool Intent 与 Tool Result 的持久化边界接收 `ToolCallStarted`、`ToolCallCompleted`，展示完整调用参数和有界结果预览；OpenAI reasoning summary 则只展示 Provider 明确返回的流式摘要。Provider 服务身份已与 `wire_protocol` 分离；Boot 支持 Anthropic Messages、OpenAI Responses 与 OpenAI-compatible Chat Completions，OpenCode Go 可在同一冻结 Package 中装载三种 wire 的 `primary/worker/utility`。Responses 请求固定 `store=false` 并由完整 ModelRequestIntent 重建，不把 `response_id` 作为恢复权威；真实模型用量由 Provider metadata 写入完整 AssistantMessage，再按 Operation 的确定分支区间投影到 Event 与 App/CLI 结果。阶段 10 已增加 SQLite v12 ModelCall、可靠内容存储、三种 Provider prepare/发送门禁、流式聚合和崩溃恢复，并移除受支持 Provider 的 `generate/stream` 绕过入口。生产清理统一经过 `ContributionScope.close()`；旧通用 Runtime/Persistence/Context 同义路径已删除。`ConversationRuntime` 的前台 task 与互斥锁已经删除；同一 live Agent 的驱动入口由 `Agent` 串行化，后台 task 与重复 wake 由 `AgentRegistry` 管理。Operation 级 LoadedPackageHandle 覆盖 accepted、waiting、resume 到终态；reload 后继续使用旧代 Package 与 Effects，终态和 shutdown 均释放引用。

当前实现为 SQLite v12。`ModelCall + RequestContent + ResponseContent` 已接管完整请求、聚合响应与 Provider 调用恢复事实；`RequestSnapshotRecord` 不再是生产权威。

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
ConversationProjector + OperationDriver token preflight + RuntimeEffects + ModelContextBuilder
  固定 leaf 与 Package → Provider-neutral ModelContext

Provider Mapper
  ModelContext → Provider wire request
```

固定准备顺序：

```text
固定 leaf 的 Conversation Tree 投影
→ token preflight；达到阈值则提交 HistoryCompaction 后重新投影
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
- 模型响应提交后从恢复状态清除完整 ModelContext；历史完整请求与聚合响应由 `ModelCall` 内容存储可靠保存，不受 trace level 影响。
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

Root 与 Child 使用同一 `Agent` 类型；每个 child 使用独立 child Session：

```text
Parent Session
├── AgentDelegation → Child Session A → Child Operation A
└── AgentDelegation → Child Session B → Child Operation B
```

child Session 使用自己 Operation 冻结的 AgentPackageVersion 和 `active_node_id`；阶段 6
实现只允许与 Parent 相同 Package，阶段 9 将其替换为 Parent 冻结 allowlist 内的 Agent
Package 选择，并把 child_package_version_id 持久化在 AgentDelegation。父子执行关系仍只
由 AgentDelegation 保存。

接口拆分为：

- `start_delegation(...) -> child_session_id`
- `wait_delegation(...)`
- `interrupt_agent(...)`

`start_delegation` 不同步等待 child 完成。阶段 6 使用 `wait_delegation` 读取 child
成功终态的唯一最终 AssistantMessage；阶段 9 改为终态主动投递，wait 降为兼容控制接口。
大结果仍可使用 Artifact；child Session 的
`active_operation_id` 和对应 AgentRunState 是执行状态权威。`report` 只做 child 到
parent 的中间通信，不结束 child、不替代最终 AssistantMessage，也不作为
`wait_delegation` 的结果。

Parent Operation 进入 `cancelling` 后，必须沿 AgentDelegation 图查询全部非终态后代，以幂等 CAS 写入 child cancellation，再按 child Session 调用 `AgentRegistry.wake()`。不能只遍历 live Agent；父取消恢复时必须重做级联检查。child 由自己的 OperationDriver 协调 Tool Intent，Parent 达到安全边界后才能终态。模型工具 `interrupt_agent` 是另一条窄控制路径：只选择目标 direct child 当前 Operation，随后按普通 cancellation 合同级联该 Operation 创建的非终态后代；保留 Session 与 AgentDelegation，不允许调用方任意选择非后代 Operation。

验收：多个 child 可以并行；单个 child 失败不污染其他 Session；进程重启后可以分别恢复；child 使用自己的冻结 Package；父取消不会遗留运行或 waiting 的孤儿 Operation，也不会删除无关用户 InboxMessage。

## 12. 阶段 7：清理（前置批次）

逐项处理只有类型、没有产品路径的能力：

| 能力 | 处理原则 |
| --- | --- |
| RequestDigestEvent | 已删除；阶段 10 由 `ModelCall` 内容引用保存完整请求/响应；当前 `RequestSnapshotRecord` 仅为过渡观测实现 |
| HostCallRecorder | 已移出公共 API；未接入真实观测路径 |
| ActivationControl | 已删除；未接入 RuntimeHost/Boot |
| AgentRunProgress | 已删除；仅有进度 DTO、没有生产发射或消费路径，不作为 Runtime 公共能力 |
| ModelStepStarted / ToolCallStarted / ToolCallCompleted | 已删除；没有生产发射路径，连同专属 ToolRenderer 与无真实来源的 ToolTiming 一并移除 |
| AgentRunUsage | 已接通；Provider metadata → 持久化 AssistantMessage → Operation 分支纯投影 → Event 与 App/CLI 结果；缺少稳定历史终点时明确返回 `None`，不误扫后续 Operation |
| `/model`、`/thinking` | 已隐藏；未实现 Environ → 新 Package → 未来 Operation 的完整切换语义前不公开 |
| Provider Boot | Anthropic Messages、OpenAI Responses 与 OpenAI-compatible Chat Completions 支持新建和冻结 Package 恢复；服务身份与冻结 `wire_protocol` 分离；Gemini 统一返回 `provider_unsupported`，仅保留直接调用适配器测试 |
| 重复 ArtifactService 注入 | 已收敛；RuntimeHost 按 Store 复用唯一实例，Boot 显式转发给 Provider 与 RuntimeEffects/ToolServices |

执行控制下沉已经完成：`ConversationRuntime` 不再保存前台 task 与互斥锁；`Agent.followup_and_wait()` 在写 Inbox 前原子判断 busy，并与 `when_idle()`、`resume_operation()` 共用 drive lock；`AgentRegistry` 继续负责后台 task 和 wake 去重。调用期间临时 `LoadedPackageHandle` 允许 Conversation detach，而不提前关闭仍在执行的 Generation。

Operation 生命周期引用已经完成：`RuntimeHost` 使用私有 `operation_id → LoadedPackageHandle` 表，waiting 保留，终态释放。headless Agent 的 Session 级 Handle 继续存活，以便同一次 drain 或后续 followup 继续工作；Conversation 接管或 Host shutdown 时再关闭。

最终校对本文引用的领域合同，更新原文，不创建同主题 v2/v3。

## 12.1 阶段 8：Tool 与 Delegation 契约收敛

阶段 0–7 的实现批次已完成，但在最终合同校对中发现 Tool 输出和 Delegation 控制面
仍有需要独立验证的语义收敛。阶段 8 不扩大实体边界，不新增 Result、Delegation
状态或通用 Manager；只替换现有接口和结果权威。

### 8.1 Tool 输出合同

- `ToolDefinition.output_schema` 为必填且随 AgentPackageVersion 冻结；它是 Runtime/Package 执行合同，Provider wire 工具投影只发送 `name`、`description`、`input_schema`。
- Tool `execute(arguments)` 只返回可序列化规范 `JSONValue`；不得返回并列的结构化结果和展示文本。
- Runtime 按 output schema 验证返回值，再调用纯 `render(validated_value)` 生成唯一模型可见 `ToolResultMessage.content`。
- 验证失败按 Provider 原始 ToolCall 顺序生成错误 ToolResult；`ToolCallState` 仍只保存结果 Node 引用。
- 删除 `structured_content` 的读写、映射和测试权威；Provider、UI、Trace 只从同一 `content` 派生展示。

验收：缺少 output schema 的 ToolDefinition 无法装载；合法 JSONValue 经过 schema
验证和 render 后只产生一个模型可见 content；非法输出进入稳定错误 ToolResult；
重启恢复和 Provider retry 不产生第二份结果内容。

### 8.2 Child 终态结果与 report 分离

- child 成功 Operation 的最终结果唯一由 `final_assistant_node_id` 指向的完整 AssistantMessage 表达。
- `report` 只向 parent Inbox 写入中间通信；不结束 child、不设置 final_assistant_node_id、不创建 result/settlement 实体。
- `wait_delegation` 有界等待 child 终态，并只从持久化最终 AssistantMessage 读取成功结果；超时不取消 child。

验收：report 前后 child 均可继续执行；child 成功时 wait 返回最终 AssistantMessage
而非 report；child 失败或取消时不伪造成功结果；重启后仍能从
`final_assistant_node_id` 读取同一结果。

### 8.3 `interrupt_agent` 控制语义

- 将模型工具 `cancel_delegation` 重命名为 `interrupt_agent`，并更新冻结 ToolDefinition、调用方和文案。
- 只校验并选择目标 direct child 当前的 active Operation，依照普通 cancellation/Tool reconciliation 安全收敛；该 Operation 创建的非终态后代继续执行既有级联取消。
- 保留 child Session 与 AgentDelegation；不归档、删除或重建关系，调用方不能任意选择非后代 Operation。

验收：非 direct child 被拒绝；CAS 竞争不会误中断新 Operation；目标 Operation 可
跨重启进入 cancelling/cancelled；中断后同一 Session/Delegation 可继续接受后续
followup；Session 与 AgentDelegation 关系保留，Inbox 处理遵循普通 cancellation 合同。

## 12.2 阶段 9：Multi-Agent 消息与 Package 选择收敛

阶段 9 替换“模型逐个调用 wait 回收 child”的协作方式。它不增加 Lane、Result、
Settlement、Manager 或第二种 Agent；Root/Child 继续使用统一 Agent 和独立 Session。

### 9.1 紧凑结果投影

- 增加一个纯投影器，成功时只读取 `final_assistant_node_id` 的 TextBlock/ArtifactBlock，失败和取消输出稳定摘要。
- 排除 ThinkingBlock、ToolCallBlock、Provider metadata、usage 和内部 ID；按 Parent Package 的 `delegation_result_max_chars` 限制文本。
- 先让 `wait_delegation` 复用投影器，输出固定为 `{timed_out, child_session_id, status, result, error}`，消除当前完整 DTO 造成的 Tool 输出膨胀。

验收：成功、失败、取消和 Artifact 的投影均有纯单元测试；同一终态重复投影字节级一致；
Tool Result 中不出现 thinking、provider metadata 或完整 child 历史。

### 9.2 Child 终态主动投递

- 扩展 SQLite/InMemory 的 Operation 终态提交：若 Session 是 delegated child，同一事务向 direct Parent 写 pending `steer`，source 为 `agent_settled`。
- settled message ID 由 child Session + Operation 稳定派生；每个 child Operation 只投递一次，后续 followup Operation 各自投递。
- commit 后由 RuntimeHost activate/wake Parent；wake 失败保留 durable Inbox，启动恢复接管。
- Parent 归档在 delegated descendants 存在非终态 Operation 或 pending Inbox 时必须拒绝；祖先已归档的 child 拒绝新消息，保证终态通知始终有合法目标。

验收：事务故障不会出现“child 已终态但通知未写入”；幂等重试不重复；Parent 运行中在
Step 边界消费、空闲时创建新 Operation；重启后未 wake 消息仍被处理。

### 9.3 模型工具面收敛

- 从 Pickle 默认配置与新构建 Package 的模型可见 Tool 集移除 `wait_delegation`。
- Host/SDK、内部测试和历史冻结 Package 保留兼容入口；Loader 仍能装载旧 ToolVersion。
- `report` 保持 Child → Parent 中间 steer；`send_message` 保持 Parent → Child followup；Parent 请求汇报直接使用 send_message，不增加 `request_report`。

验收：新模型 Context 中不存在 wait；旧 Package 可恢复；report 不结束 child；并行 child
按 settled steer 实际到达的 Inbox sequence 驱动 Parent，不出现固定 join 顺序。

### 9.4 Agent Package format 3

- AgentDefinition 增加 `AgentDelegationPolicy(default_agent_id, allowed_agent_ids)`；默认兼容策略只允许自身。
- Package format 3 冻结 DelegationPolicy 与 `delegation_result_max_chars`；Package Builder 验证默认 Agent、允许集合和已注册 Agent。
- `delegate_agent` 增加可选 `agent_id`，其 input schema 冻结为 allowlist enum；禁止 raw provider/model/tools/endpoint/secret 参数。
- Loader 继续只读兼容 format 1/2，新 Operation 使用 format 3。

验收：配置变化产生新 Package ID且不改变旧 Operation；非法/未注册 Agent 在 Tool Intent
前拒绝；模型无法越过 Package allowlist。

### 9.5 SQLite v11 与跨 Package child

- v11 只给 AgentDelegation 增加 `child_package_version_id` FK；v10 → v11 用 Parent Operation Package 回填，因为 v10 只支持 same-package child。
- `DelegateAgentIntent` 在副作用前解析并冻结目标 child Package；创建事务把同一值写入 Delegation，idle 激活和恢复只读该列。
- child 使用自身 behavior/ModelPolicy/ToolPolicy；有效 Workspace 和 Tool 权限取 Parent 已授权边界与 child 策略交集，delegated child 不得 approval 扩权。
- migration 不为历史终态 child 补发 settled 消息，避免升级后污染 Parent Inbox。

验收：Parent/Child 使用不同模型和 Tool 集仍可创建、运行、重启恢复；当前配置删除或改名
不能静默替换冻结 child Package；权限交集为空或缺少必需 Tool 时在副作用前失败。

### 9.6 端到端与提交顺序

按 `9.1 → 9.2 → 9.3 → 9.4 → 9.5` 小批次提交，每批均保持测试可运行；9.6 只补跨批
验收，不夹带新抽象。至少覆盖：两个 child 乱序完成、child 503 重试后成功、child 失败、
child 取消、Parent 忙/闲两种投递、进程在 child 终态后 wake 前崩溃、report 后继续执行、
send_message 触发 child 新 Operation、不同 Agent Package 组合和历史 Package 恢复。

真实 Provider smoke 使用已配置的 OpenCode Go/CPA 环境，普通合同测试使用确定性 Provider，
避免把外部 503 当成本地状态机失败。每批由 Luna 代理只领取一个编号，提交前报告修改文件、
合同测试、全量测试与未解决风险；顶层代理校验不得提前实现下一批。

## 12.3 阶段 10：ModelCall 可靠数据底座

阶段 10 只建立后续观测、诊断、评测和自进化可以依赖的原始事实，不实现评分器、诊断规则、
Prompt 优化、Benchmark 编排或新的 HTML 报告。`ModelRequestIntent` 继续表示逻辑请求与恢复
状态；`ModelCall` 表示一次实际 Provider 尝试，两者不得合并。

### 10.1 SQLite v12 与内容存储

- 新增 `model_calls`，按 [`数据库实体设计`](./2026-07-12-db-entities.md) 实现字段、外键、
  状态 CHECK、唯一索引和 v11 → v12 一次性迁移。
- 实现窄接口 `ModelCallContentStore`；RequestContent 与 ResponseContent 使用不可变、内容寻址
  存储，SQLite 只保存引用、fingerprint 和检索字段。
- 内容存储写入成功后，必须在同一数据库事务中完成 `request_attempt + 1` 与
  `ModelCall(status=prepared)` 插入；任一步失败都不得调用 Provider。

验收：迁移可回滚；同一 `(operation_id, step_id, request_attempt)` 不能产生两次调用；内容
缺失时明确失败，不把损坏记录伪装为完整调用。

### 10.2 Provider Prepare 与发送闸门

- 每个 Provider Mapper 只生成一个不可变 `PreparedModelCall`，其中的 Provider wire request
  同时用于 RequestContent 持久化和真实网络发送，禁止第二次映射。
- 发送前以 CAS 将 `prepared → in_flight`；CAS 失败时不得发送。
- `primary`、`worker`、`utility` 的 `generate/stream` 均通过该闸门；纯本地 token count
  不创建 ModelCall。
- Session 级 utility 调用允许 `operation_id/step_id` 为空，但必须有 `session_id`、
  `model_role` 和 `purpose`。

验收：保存失败、CAS 竞争和进程在 prepared 后崩溃均不会产生未知外部调用；Provider 收到的
请求与保存字节一致。

### 10.3 流式聚合与响应提交

- Runtime 继续实时转发 Provider-neutral `StreamDelta`；Delta Trace 仍允许丢帧，不进入恢复
  事务。
- 流结束时聚合完整 Provider 输出，包括文本、Thinking、ToolCall、usage、finish reason、
  Provider metadata 与原始响应标识，写入不可变 ResponseContent。
- ResponseContent 必须先持久化；随后在同一数据库事务中完成 ModelCall 终态、
  AssistantMessage Node 与 AgentRunState 转换。响应内容保存失败时不得提交 AssistantMessage。
- Provider HTTP/协议/超时错误写入 ModelCall 失败终态；本地持久化错误保持可区分，不能伪装
  为 Provider 失败。

验收：流式与非流式得到同构的完整 ResponseContent；工具调用参数增量可正确聚合；进程在响应
内容写入和业务提交的任一边界崩溃后，恢复结果确定。

### 10.4 恢复、清理与合同验收

- 恢复按 `ModelRequestIntent + 最新 ModelCall` 判断：无调用或 `prepared` 可安全重试；
  `in_flight` 视为外部副作用未知，进入明确协调/重试策略；终态调用不得重复发送。
- `/context` 的历史 actual request 和后续观测导出读取 ModelCall 内容；preview 仍由
  `ModelContextBuilder` 纯构建，不创建调用记录。
- 删除生产路径对 `RequestSnapshotRecord` 的完整请求权威依赖；Trace/Event/Span 继续仅用于
  导航、时序和诊断，不参与业务恢复。
- 增加 SQLite/InMemory 合同测试、三种 Provider wire 映射测试、流式聚合测试、崩溃点测试和
  至少一次配置环境下的 live smoke；外部 5xx 不得被误判为本地合同失败。

提交顺序固定为 `10.1 → 10.2 → 10.3 → 10.4`。每个批次只引入当前所需窄接口，不增加
`EvalRun`、`Diagnosis`、`EvolutionPlan`、通用 Telemetry Store 或第二套 Event Log。

## 13. 每个批次的完成标准

阶段 10 之后由真实 Session 暴露的观测口径、Parent/Child 驱动、Context 压缩与 Provider
延迟问题，统一按 [`观测驱动 Runtime 评审结论`](./2026-08-27-observation-driven-runtime-findings.md)
的 11.1–11.7 小批次继续实施；不恢复固定 5-turn Window，不增加 Child 等待状态。

当前状态（2026-08-28）：11.1–11.6 已完成。11.5 只保留可注入
`HistoryCompactionGenerator` 接缝，未内置历史选择和摘要策略；11.6 已按工具资源形态完成大型
结果验收，`read` 图片复用已有 Artifact/多模态 Provider 链路，且未增加通用分页或图片专用
Tool。11.7 仅有基础 attempt 机制，等待简单重试时序定案。

进入 11.5 前已完成一次等价架构收敛：`OperationDriver._drive_operation` 按持久化阶段
拆分；`AgentRunStateMachine` 归 `operations`；模板加载归 `templates`；
Provider wire 请求归 `providers`；诊断记录归低层 `telemetry`；旧
`wait_delegation` 只保留在 format 1/2 Package Loader 的隔离恢复实现。该批不改变
HistoryCompaction、Tool Result 或 Provider retry 业务语义。

一个批次必须同时满足：

1. 新行为有自动化测试。
2. 相关测试和全量测试通过。
3. Black 和项目约定的静态检查通过。
4. 没有新增未使用的公共类型和预留接口。
5. 没有旧、新两条生产路径长期并存。
6. 没有顺手引入下一阶段抽象。
7. 对应领域合同与实现一致；存在冲突则停止合并。
8. 提交只说明一个可独立理解的变化，并可以完整 revert。


## 15. 已锁定的实施前置

以下内容不再作为实现中的自由选择：

1. Persistence 当前使用 SQLite v12 明确领域表；v11 是 `model_calls` 的一次性迁移来源，v10 是增加 `AgentDelegation.child_package_version_id` 的历史迁移来源；AgentRunState 仍是一行当前状态，不保存历史 revision。
2. 一个 ConversationSession 同时最多一个非终态 AgentRun，以 `active_operation_id` 和接受事务保证。
3. Extension 贡献按 RuntimeGeneration 构建，ContributionScope 统一 LIFO rollback/close。
4. Session 使用 `archived_at`；公共 delete 默认拒绝关系图，显式 delete_session_tree 删除完整 child 子树。
5. Tool 副作用未知由 `intent_recorded + waiting_reason=tool_reconciliation` 表达，不增加 outcome_unknown status。
6. Root/Child 使用统一 Agent 和独立 Session，不引入 Lane。
7. Approval 使用 AgentRunState revision CAS；不增加正确性锁或盲目重试。
8. 新需求若要求改变上述任一项，先更新命中的领域合同，再修改代码。
