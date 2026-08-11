# Operation 持久化与恢复模型

**日期**：2026-08-11  
**状态**：已实施（阶段 3）
**范围**：SessionOperation、AgentRunState、ModelStepState、ToolCallState、Agent Package 绑定与恢复语义  
**不在范围**：Runtime 组件拆分、观测事件、多模态 Artifact、多 Agent 调度

命名遵循 [`Agent Runtime 重构命名约束`](./2026-08-10-agent-runtime-naming.md)，底层事务遵循 [`数据库实体设计`](./2026-07-12-db-entities.md)。

## 1. 结论

```text
SessionOperation（不可变身份记录）
├── agent_package_version_id
├── accepted_commit_sequence
└── operation/<operation_id>/state（NamedReference）
    └── ImmutableObject(session_operation_state)
        └── AgentRunState
            └── ModelStepState
                └── ToolCallState[]
```

- Operation 被 Session 接受时即持久化，不能先启动协程再补记录。
- 每次状态转换创建新的不可变 State，并移动唯一状态引用。
- 恢复只读取最新状态引用，不扫描 Event 或重放 reducer。
- 一个 AgentRun 在接受时绑定一个已持久化的 `AgentPackageVersion`。
- `operation_id`、`step_id`、`tool_call_id` 分别是三个层级的身份；序号只排序，不充当身份。

## 2. `session_operations`

| 列 | 类型 | 约束 | 含义 |
| --- | --- | --- | --- |
| `operation_id` | TEXT | PK | Operation 稳定身份 |
| `session_id` | TEXT | FK → sessions | 所属会话 |
| `operation_type` | TEXT | `agent_run` 等 | 工作类型 |
| `agent_package_version_id` | TEXT | FK → agent_package_versions，AgentRun 必填 | 接受时冻结的 Agent Package |
| `accepted_commit_sequence` | INTEGER | FK → storage_commits | 接受操作的原子提交 |
| `created_at` | TEXT | NOT NULL | UTC ISO8601 |

第一版只接受 `agent_run`。后续 HistoryCompaction、HistoryNavigation 和 Delegation 复用同一 Operation 身份，不另建 Turn/Job/Task 表。

## 3. 接受事务

一次 AgentRun 接受提交必须共享同一个 `commit_sequence`：

```text
insert SessionOperation
insert UserMessage ImmutableObject
append ConversationNode
move conversation/active
insert initial AgentRunState ImmutableObject
move operation/<operation_id>/state
commit
```

任一步失败则 Operation、用户消息、状态与 Reference 全部不可见。禁止接受后仍使用未绑定 Package 的“当前配置”。

## 4. 状态对象

`session_operation_state@1` 的持久化内容：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `operation_id` | string | 所属 Operation |
| `operation_type` | `agent_run` | 状态种类 |
| `revision` | integer | 从 1 递增 |
| `status` | enum | `queued/running/waiting/succeeded/failed/cancelled` |
| `user_message_node_id` | string | 接受事务写入的用户消息节点 |
| `current_step` | ModelStepState/null | 当前模型步骤 |
| `completed_step_ids` | string[] | 已完成步骤的稳定身份 |
| `final_assistant_node_id` | string/null | 成功终态的回答节点 |
| `error` | object/null | 可恢复的失败摘要，不保存 Python exception |
| `model_context_feedback` | string[] | 已持久化、待注入后续 ModelContext 的 Hook 反馈 |

`ModelStepState`：

| 字段 | 含义 |
| --- | --- |
| `step_id` | 稳定身份 |
| `step_sequence` | Operation 内从 1 递增的显示顺序 |
| `phase` | Provider/Tool Loop 当前持久化阶段 |
| `assistant_message_node_id` | 已落盘的模型输出节点，可空 |
| `tool_calls` | 有序 ToolCallState 列表 |
| `retry_count` | 已持久化的模型请求重试次数 |
| `post_tool_batch_hook_completed` | PostToolBatch Hook 是否已经跨越执行边界 |

`ToolCallState` 还持久化 `execution_policy`、`decision_reason`、结果节点与错误标记。Hook 对参数和执行策略的决定因此不是进程内临时状态，恢复后不会重新询问模型来猜测。

状态 Reference 使用 `operation/<operation_id>/state`。State 中的 `revision` 与 Reference 的 `commit_sequence` 含义不同：revision 只排序该 Operation 的状态版本；Reference `commit_sequence` 是 Session 的持久化提交顺序。

## 5. Tool 恢复语义

| ToolCallState | 崩溃后语义 |
| --- | --- |
| `ready` | 尚未跨越执行边界；恢复后可记录 intent 并执行 |
| `intent_recorded` | 外部结果未知；默认禁止自动重放 |
| `completed` | 结果消息已持久化；继续后续 Tool Loop |

安全顺序固定为：

```text
ToolCallReady
→ commit ToolCallIntentRecorded
→ execute external tool
→ atomically append ToolResultMessage + commit ToolCallCompleted
```

`intent_recorded` 不等于“工具一定执行过”，只表示真实世界副作用可能已经发生。恢复时：

1. 工具有稳定幂等键和显式 reconcile 能力时，可查询结果后提交 completed；
2. 否则暂停等待 Host 决策，或写入明确的中断 ToolResult；
3. 禁止 Runtime 猜测“应该没执行”并静默重放。

Host 核实外部结果后调用 `record_reconciled_tool_result()`，结果消息与 `waiting → running` 状态转换在同一提交中完成；随后 `resume_operation()` 从已完成 ToolCall 继续。Host 也可以通过 `cancel_operation()` 明确结束 Operation。

这就是 Operation Record 支持续执行的边界：它能确定从哪个状态继续，也能确定哪些动作不能安全自动继续；它不承诺恢复已经丢失的 Python 协程。

## 6. 状态转换约束

- 新状态 `revision = current.revision + 1`。
- 提交同时校验 Session `current_commit_sequence` 与状态 Reference 当前 `commit_sequence`。
- 终态不能回到运行态。
- `succeeded` 必须有 `final_assistant_node_id`。
- `failed/cancelled` 不得留下可自动执行的 `ready` ToolCall。
- State 只能引用同 Session、已存在或同事务新建的 Node。
- AgentRun 的 Package ID 创建后不可改变。

## 7. 阶段 3 验收

1. 接受 AgentRun 时 Operation、用户消息、初始 State 和两个 Reference 原子成功或全部回滚。
2. Package Version 不存在时拒绝接受。
3. State revision 与 Reference CAS 冲突可复现，失败不消耗 `commit_sequence`。
4. 进程重启后按 `operation/<id>/state` 一次读取恢复最新 AgentRunState。
5. `intent_recorded` ToolCall 不被自动重放。
6. 核心持久化身份不再新增 `turn_id`。
7. Host 可原子补交未知 ToolCall 结果并继续，或显式取消 Operation。
