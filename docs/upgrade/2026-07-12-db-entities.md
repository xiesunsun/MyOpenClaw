# 数据库实体设计

**初稿日期**：2026-07-12
**更新日期**：2026-08-11
**状态**：已实施（SQLite schema v9）
**范围**：SQLite 中的会话、不可变对象、会话节点、可移动引用、Package、Operation、Artifact 元数据、Agent Delegation 和原子提交
**不在范围**：Artifact Blob 实现细节、Operation 状态字段和多 Agent 调度策略

本文定义持久化事实和事务边界。实体名称遵循 [`2026-08-10-agent-runtime-naming.md`](./2026-08-10-agent-runtime-naming.md)；旧 `SessionEntry`、`leaf_id` 和 `session_entries` 合同已被本文替代。

## 1. 目标模型

```text
ConversationSession
├── StorageCommit(sequence)
├── ConversationNode ──► ImmutableObject
├── NamedReference ─────► ConversationNode | ImmutableObject
└── SessionOperation ───► AgentPackageVersion

ConversationEntry = ConversationNode + ImmutableObject（只读投影，不落库）
```

| 实体 | 是否可更新 | 含义 |
| --- | --- | --- |
| `ConversationSession` | 仅封面状态可变 | 会话身份、归属和当前提交序号 |
| `StorageCommit` | 否 | Session 内一次成功原子提交 |
| `ImmutableObject` | 否 | 版本化 JSON 内容 |
| `ConversationNode` | 否 | Object 在会话树中的位置 |
| `NamedReference` | 以追加新版本移动 | 指向 Node 或 Object 的具名指针 |
| `ConversationEntry` | 不落库 | Node 与 Object 解析后的读取视图 |
| `SessionOperation` | 否 | Session 接受的工作身份；状态内容见 Operation 恢复合同 |
| `Artifact` | 否 | 多模态二进制的内容寻址元数据；字节不进入 SQLite |

## 2. 共享 commit_sequence

`commit_sequence` 是单个 Session 内的持久化提交顺序，从 `1` 严格递增。

- 一个 `StorageTransaction` 只分配一个 `commit_sequence`。
- 同一事务插入的 Object、Node 和 Reference 版本共享该序号。
- 事务回滚不得消耗 `commit_sequence`。
- `commit_sequence` 用于审计、增量 Watch 和并发检查，不用于扫描历史恢复当前状态。
- EventBus 另行分配进程内 `event_sequence`；二者不可比较，也不能互相充当 cursor。

## 3. 表结构

### 3.1 `sessions`

| 列 | 类型 | 约束 | 含义 |
| --- | --- | --- | --- |
| `session_id` | TEXT | PK | UUID |
| `agent_id` | TEXT | NOT NULL | 创建时使用的 Agent Definition 标识 |
| `cwd` | TEXT | NOT NULL | 规范化工作目录 |
| `current_commit_sequence` | INTEGER | NOT NULL, DEFAULT 0 | 最近成功提交序号 |
| `created_at` | TEXT | NOT NULL | UTC ISO8601 |
| `updated_at` | TEXT | NOT NULL | UTC ISO8601 |
| `status` | TEXT | NOT NULL | `active` / `archived` |
| `title` | TEXT | NULL | 展示标题 |

`sessions` 不保存 `leaf_id`。活动节点由 `conversation/active` NamedReference 唯一表达。

### 3.2 `storage_commits`

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `session_id` | TEXT | FK → sessions |
| `commit_sequence` | INTEGER | Session 内递增 |
| `commit_id` | TEXT | 全局唯一 UUID |
| `committed_at` | TEXT | UTC ISO8601 |

主键：`(session_id, commit_sequence)`；`commit_id` 唯一。

### 3.3 `immutable_objects`

| 列 | 类型 | 约束 | 含义 |
| --- | --- | --- | --- |
| `object_id` | TEXT | PK | UUID |
| `object_type` | TEXT | NOT NULL | 如 `agent_message`、`history_compaction` |
| `schema_version` | INTEGER | NOT NULL | 对象自身 schema 版本 |
| `digest` | TEXT | NOT NULL | 规范 JSON envelope 的 SHA-256 |
| `content_json` | TEXT | NOT NULL | JSON object |
| `created_session_id` | TEXT | NOT NULL | 首次创建所在 Session |
| `created_commit_sequence` | INTEGER | NOT NULL | 首次创建所在提交 |
| `created_at` | TEXT | NOT NULL | UTC ISO8601 |

对象只能 INSERT，禁止 UPDATE。第一阶段不按 digest 自动合并对象；digest 用于校验和后续内容寻址。

### 3.4 `conversation_nodes`

| 列 | 类型 | 约束 | 含义 |
| --- | --- | --- | --- |
| `node_id` | TEXT | PK | UUID |
| `session_id` | TEXT | FK → sessions | 所属会话 |
| `parent_node_id` | TEXT | NULL | 同 Session 父节点；根节点为空 |
| `object_id` | TEXT | FK → immutable_objects | 节点内容 |
| `created_commit_sequence` | INTEGER | NOT NULL | 创建节点的提交序号 |
| `created_at` | TEXT | NOT NULL | UTC ISO8601 |

节点只能 INSERT。`parent_node_id` 必须为空或指向同 Session 已存在/同事务新建的节点。

### 3.5 `named_references`

NamedReference 的移动通过追加版本表达，不覆盖旧行。

| 列 | 类型 | 约束 |
| --- | --- | --- |
| `session_id` | TEXT | FK → sessions |
| `reference_name` | TEXT | 如 `conversation/active` |
| `commit_sequence` | INTEGER | 本次移动所在提交 |
| `target_kind` | TEXT | `node` / `object` |
| `target_id` | TEXT | 目标 ID |

主键：`(session_id, reference_name, commit_sequence)`。当前 Reference 是同名行中 `commit_sequence` 最大的一条。

### 3.6 `agent_package_versions`

| 列 | 类型 | 约束 | 含义 |
| --- | --- | --- | --- |
| `package_version_id` | TEXT | PK | `agentpkg_<digest>` |
| `digest` | TEXT | UNIQUE | Snapshot 内容 SHA-256 |
| `agent_id` | TEXT | NOT NULL | Pickel 设置中的 Agent ID |
| `content_json` | TEXT | NOT NULL | 不含密钥的完整 Package Snapshot |
| `created_at` | TEXT | NOT NULL | UTC ISO8601 |

Package Snapshot 直接从 Pickel 现有 `AppConfig / AgentConfig / ModelConfig / Skill / ToolBus` 解析；不引入第二套配置文件。相同内容重复插入幂等，ID 相同但内容不同必须失败。

### 3.7 `session_operations`

Operation 身份表使用 `operation_id` 主键，并以 `(session_id, accepted_commit_sequence)` 关联接受它的 `StorageCommit`，以 `agent_package_version_id` 关联冻结的 Package。状态不覆盖此行，而是通过 `operation/<operation_id>/state` NamedReference 指向不可变 State；完整字段和转换约束见 [`Operation 持久化与恢复模型`](./2026-08-11-operation-recovery-model.md)。

### 3.8 `artifacts` 与 BlobStore

| 列 | 类型 | 含义 |
| --- | --- | --- |
| `artifact_id` | TEXT PK | `artifact_<sha256>` 稳定身份 |
| `digest` | TEXT | Blob 字节 SHA-256 |
| `media_type` | TEXT | Provider-neutral MIME type |
| `size_bytes` | INTEGER | Blob 大小 |
| `blob_key` | TEXT | BlobStore 内部内容寻址键 |
| `created_at` | TEXT | UTC ISO8601 |

SQLite 只保存不可变元数据；实际字节由 `BlobStore` 保存。默认本地实现使用 `sha256/<前两位>/<剩余 digest>`，以后可以替换为对象存储而不改变 `ArtifactReference` 和消息 schema。消息 payload v3 使用 `ArtifactBlock(artifact=ArtifactReference)`，禁止直接持久化 base64、临时 URL 或 Provider 文件 ID。Blob 先写、元数据后写，失败可能留下可 GC 的孤立 Blob，但绝不能留下指向不存在 Blob 的已提交消息。

### 3.9 `agent_delegations`

| 列 | 类型 | 含义 |
| --- | --- | --- |
| `delegation_id` | TEXT PK | 委派关系稳定身份 |
| `parent_operation_id` | TEXT FK | 发起委派的父 AgentRun |
| `parent_step_id` | TEXT | 发起委派的父 ModelStep |
| `parent_tool_call_id` | TEXT NULL | 若通过工具发起，记录父 ToolCall |
| `child_operation_id` | TEXT UNIQUE FK | 被委派的 AgentRun |
| `child_session_id` | TEXT | child 所属隔离 Session |
| `created_commit_sequence` | INTEGER | child Operation 接受提交 |
| `created_at` | TEXT | UTC ISO8601 |

`AgentDelegation` 与 child `SessionOperation` 必须在 child Session 的同一 StorageTransaction 中创建。一个 child Operation 只能有一个父关系。Lane 只表示同一会话树上的独立活动位置，不表示 Agent；动态子 Agent 默认创建隔离 Session，避免多个执行者竞争同一 `conversation/active`。

## 4. StorageTransaction

唯一写边界：

```python
transaction = store.begin_storage_transaction(
    session_id=session_id,
    expected_commit_sequence=current_commit_sequence,
)
object_id = transaction.insert_immutable_object(...)
node_id = transaction.append_conversation_node(
    object_id=object_id,
    parent_node_id=active_node_id,
)
transaction.move_named_reference(
    reference_name="conversation/active",
    target_kind="node",
    target_id=node_id,
    expected_current_commit_sequence=active_reference_commit_sequence,
)
commit = transaction.commit()
```

事务必须：

1. 校验 Session 当前 `commit_sequence` 与调用方预期一致；
2. 校验 Object、Node 和 Reference 目标；
3. 分配 `current_commit_sequence + 1`；
4. 写入全部不可变事实和 Reference 新版本；
5. 插入 StorageCommit；
6. 更新 Session `current_commit_sequence/updated_at`；
7. 任一步失败则全部回滚且不消耗 `commit_sequence`。

Reference move 还需校验 `expected_current_commit_sequence`：

- `None` 表示调用方预期该 Reference 尚不存在；
- 整数表示调用方最后读取的 Reference 版本；
- 不匹配时抛并发冲突，禁止静默覆盖。

## 5. 会话树写入

追加用户消息、Assistant 消息、工具结果、Compaction 和 HostCall 的底层动作完全相同：

```text
insert ImmutableObject
→ append ConversationNode(parent = 当前 active node)
→ move conversation/active
→ commit
```

业务方法使用明确名称：

- `append_user_message()`
- `append_assistant_message()`
- `append_tool_result_message()`
- `append_history_compaction()`
- `append_host_call_request()`
- `append_host_call_response()`
- `move_active_branch_to()`

禁止先修改内存 Session 再 flush。成功提交后由 Store 返回新的 `ConversationEntry` 和 Session 只读视图。

## 6. 读取与投影

`list_active_branch_entries(session_id)`：

1. 读取最新 `conversation/active` Reference；
2. 使用递归 CTE 从 active node 沿 parent 回溯；
3. 根到叶排序；
4. JOIN ImmutableObject；
5. 返回 `ConversationEntry`。

`ConversationEntry` 只包含读取所需的 Node 与解析对象，不拥有独立 Repository，也不能写回数据库。

Context、Session Preview 与 OpenViking 都消费同一个活动分支读取接口，禁止分别重建树遍历逻辑。

`ConversationProjector.project_conversation_messages()` 是 Context 的唯一消息投影算法。压缩对象使用 `history_compaction`，并以 `first_kept_node_id` 引用当前分支上的 `ConversationNode`；不再创建指向旧 `SessionEntry` 的 `first_kept_entry_id`。

## 7. 并发与损坏数据

- 一个 Session 同时只允许一个期望 sequence 成功提交；其他提交收到冲突并重新读取。
- Node、Object 重复 ID 必须失败。
- Reference 目标不存在、跨 Session parent、Object content 非 JSON object 必须失败。
- digest 与读取内容不一致视为存储损坏。
- parent 环无法由正常 append 产生；读取检测到环时必须报错。

## 8. 删除与归档

- 归档只改变 Session status，不删除事实。
- 删除 Session 在一个事务中级联删除 Commit、Node 和 Reference。
- ImmutableObject 随创建 Session 删除；全局 Artifact 元数据、Blob 与 AgentPackageVersion 不随单个 Session 级联删除，后续由引用扫描 GC。
- 禁止删除单个 Node、Object、Commit 或 Reference 历史版本。

## 9. Schema 策略

当前 Runtime 使用 `PRAGMA user_version = 9`，包含 `agent_package_versions`、`session_operations`、`artifacts` 与 `agent_delegations`。运行时只接受空库或 v9，不在请求路径中逐版补表。

- Runtime 不提供 v3/v4 双读或双写。
- 当前预发布阶段默认使用新库。
- 若需要保留 v3 数据，只提供一次性、事务化离线迁移；迁移完成后删除旧表，不在 Runtime 中保留兼容分支。
- schema 版本不支持时必须明确报错，不能在旧表上静默补列。

## 10. 验收

1. Object、Node、Reference 和 Commit 原子成功或全部回滚。
2. 失败事务不消耗 sequence。
3. Reference CAS 冲突可复现且不会覆盖较新指针。
4. 活动分支、分叉与移动 Reference 的读取顺序正确。
5. Context、Preview、OpenViking 共用活动分支读取接口。
6. 代码中删除 `Session`、`SessionEntry`、`leaf_id`、`active_path()` 和旧 Repository。
7. SQLite 中删除 `session_entries`，不保留运行期兼容路径。
