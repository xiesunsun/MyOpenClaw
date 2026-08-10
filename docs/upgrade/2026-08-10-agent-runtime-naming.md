# Agent Runtime 重构命名约束

**日期**：2026-08-10
**状态**：已对齐，作为后续重构的命名合同
**范围**：Agent Runtime、持久化实体、执行状态、多模态与多 Agent 的代码命名
**不在范围**：数据库表结构定稿、状态机全部迁移、兼容方案和实施排期

本文只定义“一个概念叫什么、负责什么”。后续设计和实现出现同义名称时，以本文为准；历史设计稿仍可说明当时的实现，但不再决定目标态命名。

## 1. 命名原则

1. 一个概念只有一个名称，一个名称只表达一种生命周期。
2. 实体名说明“它是什么”，方法名说明“它做什么”。
3. 跨模块类型使用完整名称，不用脱离上下文后含义不明的缩写。
4. `State`、`Event`、`Snapshot`、`Definition`、`Version`、`Reference` 各有固定语义，禁止混用。
5. 不为迁就旧代码保留永久别名；迁移完成后删除旧名。
6. 禁止用 `Manager`、`Processor`、`Handler`、`Util`、`Object`、`Context` 掩盖多种职责。
7. 底层存储可以使用通用类型，但领域层必须暴露带业务含义的类型和方法。

固定后缀的含义：

| 后缀 | 只表示 |
| --- | --- |
| `State` | 可持久化、可恢复的当前执行状态 |
| `Event` | 已发生、供订阅者观察的通知 |
| `Snapshot` | 在明确边界捕获的完整只读视图 |
| `Definition` | 用户可以编辑的源定义 |
| `Version` | 不可修改、可稳定引用的已解析版本 |
| `Reference` | 可以移动、指向另一持久化实体的引用 |
| `Entry` | Node 与内容解析后的只读视图 |
| `Operation` | 已被 Session 接受、可恢复且有终态的工作 |
| `Run` | 一次用户输入到最终回答的 Agent 执行 |
| `Step` | AgentRun 内的一次模型请求、响应和工具批次 |

## 2. 执行层级

```text
ConversationSession
└── SessionOperation
    ├── AgentRun
    │   └── ModelStep
    │       └── ToolCall
    ├── HistoryCompaction
    └── HistoryNavigation
```

| 名称 | 唯一含义 | 持久化 |
| --- | --- | --- |
| `ConversationSession` | 一棵可分支的会话树及其活动位置 | 是 |
| `SessionOperation` | Session 接受的一次可恢复操作 | 是 |
| `AgentRun` | 一次用户输入到最终回答的完整执行 | 是 |
| `ModelStep` | 一次模型请求、响应及其工具批次 | 通过 OperationState 表达 |
| `ToolCall` | 一次具体工具调用 | 是 |
| `HistoryCompaction` | 一次会话历史压缩 | 是 |
| `HistoryNavigation` | 一次会话树位置移动 | 是 |

`Turn` 不再作为 Runtime 核心执行实体。界面或统计可以把一问一答投影为 conversation turn，但持久化、事件和恢复统一使用上表术语。

持久化操作统一使用 `operation_id`。模型步骤使用 `step_id`，工具调用使用 `tool_call_id`；不再为同一执行同时维护 `turn_id`、`run_id` 和 `operation_id`。

## 3. Runtime 与 Agent

### 3.1 Runtime 层级

| 名称 | 职责 |
| --- | --- |
| `RuntimeHost` | 进程级入口，创建和管理活动会话 |
| `ConversationRuntime` | 一个活动 Session 面向 Host 的控制与观察接口 |
| `AgentRuntime` | 接受并驱动 SessionOperation 的核心运行引擎 |
| `RuntimeBindings` | Provider、工具、Hook 等进程内实现的只读绑定 |
| `RuntimeBus` | Runtime 的事件与 Host Call 组合边界；保留现名 |

`AgentRuntime` 不能成为新的依赖资源袋。它持有窄接口并协调 Operation；Provider、工具和 Hook 的具体实现通过 `RuntimeBindings` 提供。

### 3.2 Agent 层级

| 名称 | 职责 |
| --- | --- |
| `AgentDefinition` | 从用户文件读取的、可编辑的 Agent 源定义 |
| `AgentPackageVersion` | 解析完成、不可修改、可按 ID 或 digest 引用的 Agent 版本 |
| `LoadedAgentPackage` | 当前进程中已解析出工具和 Skill 实现的 Package |

每个 `AgentRun` 在接受时绑定确定的 `AgentPackageVersion`。运行中修改 Definition 不得改变已经开始的 Operation。

现有的 `Provider`、`UserMessage`、`AssistantMessage`、`ToolCall`、`HostCall` 和 `RuntimeBus` 已能直接表达职责，继续保留；禁止为了形式统一做无意义改名。

## 4. 持久化实体

存储底层采用不可变对象、会话节点、可移动引用和原子事务：

| 名称 | 含义 |
| --- | --- |
| `ImmutableObject` | 创建后不可更新的 JSON 对象 |
| `ConversationNode` | 内容在会话树中的位置 |
| `NamedReference` | 指向 Object 或 Node 的可移动持久化引用 |
| `StorageTransaction` | 原子提交的一组 Object、Node 和 Reference 变化 |
| `ConversationEntry` | ConversationNode 与其内容解析后的只读视图 |

```text
NamedReference ──► ImmutableObject
               └► ConversationNode ──► ImmutableObject
```

`sequence` 表示 Session 内的提交顺序。共享 sequence 用于事务排序、审计和 Watch cursor，不通过扫描历史恢复当前 Operation。

当前 Operation 的唯一恢复入口是：

```text
OperationStateReference
└── SessionOperationState
    ├── AgentRunState
    ├── HistoryCompactionState
    └── HistoryNavigationState
```

每次状态转换创建新的不可变 State，并移动 `OperationStateReference`。旧 State 保留用于审计，但恢复不执行历史 reducer。

### 4.1 会话树字段与方法

| 当前名称 | 目标名称 |
| --- | --- |
| `Session` | `ConversationSession` |
| `SessionEntry`（持久化节点） | `ConversationNode` |
| Node 与内容的组合结果 | `ConversationEntry` |
| `leaf_id` | `active_node_id` |
| `active_path()` | `list_active_branch_entries()` |
| `move_leaf()` | `move_active_branch_to()` |
| `append_user()` | `append_user_message()` |
| `append_assistant()` | `append_assistant_message()` |

若以后引入共享历史的 Lane，活动位置命名为 `SessionLane.active_node_id`。不再并列使用 `leaf_id`、`head_id`、`cursor_id` 和 `current_entry_id`。

## 5. Operation 状态与副作用

### 5.1 状态名称

Provider 请求使用：

```text
ModelRequestReady
ModelRequestIntentRecorded
ModelRequestRetryScheduled
ModelRequestCompleted
```

工具调用使用：

```text
ToolCallReady
ToolCallIntentRecorded
ToolCallCompleted
```

`IntentRecorded` 表示“执行意图已经持久化，但外部操作是否已经发生并不确定”。禁止用 `started` 表示这个状态，也不使用含义不明的 `effect_pending`。

### 5.2 执行组件

| 名称 | 只负责 |
| --- | --- |
| `OperationDriver` | 推进 Operation，直到暂停或结束 |
| `OperationStateMachine` | 校验状态并决定合法转换 |
| `RuntimeEffects` | 持久化、模型、工具、Hook 和 Timer 等副作用 |
| `ModelContextBuilder` | 构造 Provider-neutral 的 ModelContext |
| `ConversationProjector` | 将会话分支投影成模型可见消息 |
| `AnthropicRequestMapper` | 将 ModelContext 映射为 Anthropic 请求 |
| `ToolCallExecutor` | 准备、校验并执行 ToolCall |

推荐方法名：

```python
decide_next_action()
drive_operation()
resume_operation()
cancel_operation()

build_model_context()
project_conversation_messages()

commit_operation_state()
execute_model_request()
execute_tool_call()
invoke_hook()
wait_until()
```

### 5.3 Workflow

可靠推进属于 `OperationDriver`，不能由 Strategy 负责。模型如何思考主要由 Agent Package、Prompt、Context 和工具合同约束。

如果未来确实需要显式、可替换的流程约束，统一命名为 `RunWorkflow`，例如 `PlanAndExecuteWorkflow`。默认 Tool Loop 是 AgentRun 的基础执行语义，不命名为 `ReActStrategy`，也不与 `AgentLoop`、`ExecutionStrategy` 建立多套同义抽象。

## 6. 多模态与 Artifact

消息由明确的 Block 组成：

```text
MessageBlock
├── TextBlock
├── ArtifactBlock
├── ThinkingBlock
└── ToolCallBlock
```

| 名称 | 含义 |
| --- | --- |
| `Artifact` | 图片、音频、视频、文件或其他二进制生成物的持久化元数据 |
| `ArtifactReference` | Message 或 Tool Result 对 Artifact 的稳定引用 |
| `ArtifactBlock` | 消息中的多模态内容块，内部持有 ArtifactReference |
| `BlobStore` | 按 digest 保存和读取实际字节 |

`ArtifactReference` 使用完整名称，不缩写为 `ArtifactRef`。Provider Adapter 负责将 Artifact 转为 base64、URL 或 Provider 文件引用；领域消息不直接保存 Provider 专有格式。

## 7. 多 Agent

Lane 表示共享历史上的独立活动位置，不表示 Agent。Subagent 使用隔离的 Session 或 AgentRun：

| 名称 | 含义 |
| --- | --- |
| `SessionLane` | 共享同一会话树的独立活动位置 |
| `SessionFork` | 复制会话分支形成的新 Session |
| `AgentDelegation` | 父 Operation 与被委派 AgentRun 的持久化关系 |
| `DelegatedAgentRun` | 由另一个 AgentRun 发起的子 AgentRun |

推荐方法名：

```python
fork_session()
start_delegated_run()
wait_for_delegated_run()
cancel_delegated_run()
```

不使用无法说明创建对象的 `spawn()`、`child()`、`subtask()` 或 `fork_agent()`。

## 8. 动词约束

| 动词 | 固定语义 |
| --- | --- |
| `create` | 创建有身份的领域实体 |
| `build` | 从确定输入组装无副作用值对象 |
| `resolve` | 按规则选择并校验一个实现或配置 |
| `load` | 从持久层读取已知身份的实体 |
| `find` | 按条件查找零或一个实体 |
| `list` | 返回有顺序的实体集合 |
| `insert` | 写入不可变实体，重复 ID 必须失败 |
| `append` | 在有顺序的结构末端增加内容 |
| `move` | 改变 NamedReference 的指向 |
| `commit` | 原子提交事务或状态转换 |
| `project` | 从持久事实派生只读视图 |
| `execute` | 发生真实计算或外部调用 |
| `drive` | 重复推进状态机直到暂停或终止 |

避免单独使用 `save()`、`update()`、`write()`、`set()`、`process()`、`handle()`。必须从方法名看出目标实体和语义，例如 `commit_operation_state()`、`move_reference()`。

## 9. 当前代码迁移映射

| 当前名称 | 目标名称或处理 |
| --- | --- |
| `Agent` | `AgentDefinition`；运行时版本另建 `AgentPackageVersion` |
| `Run` | 拆为 `AgentRuntime` 与 `RuntimeBindings` |
| `RuntimeConversation` | `ConversationRuntime` |
| `TurnState` | `AgentRunState` |
| `StepState` | `ModelStepState` |
| `turn_id` | `operation_id` |
| `ExecutionStrategy` | 删除；有真实流程需求时使用 `RunWorkflow` |
| `ReActStrategy` | 拆入 OperationDriver、状态机、Context、Effects 和 Tool 执行组件 |
| `ContextAssembler` | 删除 |
| `prepare()` | `build_model_context()` |
| `SessionEntry` | 拆为 `ConversationNode` 与 `ConversationEntry` |
| `ImageContent` | `ArtifactBlock` + `ArtifactReference` |
| `TextContent` | `TextBlock` |
| `ThinkingContent` | `ThinkingBlock` |
| `ToolCallContent` | `ToolCallBlock`；运行时执行实体使用 `ToolCall` |

这张表描述目标态，不要求机械地逐类改名。遇到职责混合时必须先拆分，再命名；禁止把旧类原封不动改成新名字。

当前关键方法的目标映射：

| 当前方法 | 目标方法或处理 |
| --- | --- |
| `Run.open()` | 由 Composition Root 创建 `AgentRuntime` 和 `RuntimeBindings` |
| `Run.reload()` | `RuntimeHost.reload_agent_runtime()` |
| `Run.turn()` | `AgentRuntime.start_agent_run()` |
| `ExecutionStrategy.execute()` | 删除，由 `OperationDriver.drive_operation()` 接管推进 |
| `ContextAssembler.assemble()` | 删除 |
| `prepare()` | `ModelContextBuilder.build_model_context()` |
| `Session.active_path()` | `ConversationSession.list_active_branch_entries()` |
| `Session.move_leaf()` | `ConversationSession.move_active_branch_to()` |

## 10. 验收约束

完成相关重构时至少满足：

1. 代码中不存在同时表示同一执行的 `turn_id`、`run_id`、`operation_id`。
2. `AgentRuntime` 不直接实现 Context 组装、Provider 映射和工具执行细节。
3. `ConversationNode` 不内嵌可变运行状态。
4. `ConversationEntry` 只作为读取视图，不作为第二份持久化真源。
5. `RuntimeEffects` 之外的 Operation 过程代码不直接执行 Provider、工具或 Hook。
6. `AgentPackageVersion` 和 `ArtifactReference` 都可脱离进程内对象稳定恢复。
7. 删除完成迁移的旧名和兼容别名，测试、事件和文档同步采用新名称。
