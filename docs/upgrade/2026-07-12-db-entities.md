# 数据库实体设计（升级目标）

**日期**: 2026-07-12  
**范围**：只描述 **要落库、可从 SQLite 还原** 的实体与落库层读写合同。  
**不在本文**：TurnSnapshot、ModelContext、MemoryHit、RunDeps、Agent 整对象；OpenViking 同步状态机与 runtime 投影实现细节。

**升级策略**：新 schema 为目标态。**不做旧库兼容迁移**（含 openviking 游标、`session_messages` 历史行）。旧库可整体弃用或仅人工导出；实现以空库 / 新库为准。

---

## 1. 库里有什么

两张表 + 可选 schema 版本：

```text
sessions              一本对话（封面 + 当前指针）
session_entries       对话树里的每一行（append-only）
```

```text
sessions 1 ── * session_entries
         leaf_id ──► 某条 session_entries.entry_id（可空）
```

关系语义：

| 关系 | 含义 |
|------|------|
| `sessions.session_id` → 多行 entries | 归属 |
| `session_entries.parent_id` → 同会话另一 `entry_id` | 树边；根为 `NULL` |
| `sessions.leaf_id` → 某 `entry_id` | **当前活动路径**的末端 |

---

## 2. 约定（全局）

| 项 | 约定 |
|----|------|
| 主键 id | `session_id`、`entry_id` 均为 **全局唯一** TEXT，生成用 UUID4（字符串形式） |
| 时间 | TEXT，**UTC**，ISO8601 且带偏移，例如 `2026-07-12T08:00:00+00:00` |
| `status` | 仅 `active` \| `archived`；新建默认 `active` |
| `entry_type` | 关闭集合，见 §4；未知类型读库时保留行，投影时跳过 |
| 外键强度 | `session_id` 建议声明 FK；`parent_id` / `leaf_id` **不强制 SQLite FK**（由应用校验：同 `session_id`、目标存在） |
| schema 版本 | 使用 SQLite `PRAGMA user_version`（或单行 meta 表）；本设计为 **version = 2**（旧线性库视为 1，不自动升） |

---

## 3. 表 `sessions`

对应实体名：**Session**

| 列名 | 类型建议 | 可空 | 说明 |
|------|----------|------|------|
| `session_id` | TEXT | 否 | 主键，UUID4 |
| `agent_id` | TEXT | 否 | 归属哪个 Agent（配置里的 id，如 Pickle）；**创建后不可改** |
| `leaf_id` | TEXT | 是 | 当前指针 → `session_entries.entry_id`；空 = 尚无 entry |
| `created_at` | TEXT | 否 | UTC ISO |
| `updated_at` | TEXT | 否 | UTC ISO；任意封面或 append 成功时刷新 |
| `status` | TEXT | 否 | `active` / `archived` |
| `title` | TEXT | 是 | 显示名；可后续填写 |

**主键**：`session_id`  
**索引**：`(agent_id, updated_at)` 便于按 agent 列会话  

### 3.1 哪些列可 UPDATE

| 列 | 可否改 | 场景 |
|----|--------|------|
| `leaf_id` | 是 | append 推进；用户切分支 |
| `updated_at` | 是 | 每次成功写路径 |
| `status` | 是 | 归档 / 恢复 |
| `title` | 是 | 重命名、首条 user 生成标题 |
| `agent_id` | **否** | 会话归属固定 |
| `session_id` / `created_at` | **否** | 身份与创建时刻固定 |

### 3.2 不进这张表

- 消息正文与任何 entry payload  
- openviking 账号 / remote session / 同步游标（扩展位在 `entry_type=openviking`）  
- 模型配置、tools、system prompt  

---

## 4. 表 `session_entries`

对应实体名：**SessionEntry**

| 列名 | 类型建议 | 可空 | 说明 |
|------|----------|------|------|
| `entry_id` | TEXT | 否 | 主键，全局唯一 UUID4 |
| `session_id` | TEXT | 否 | 所属会话 → `sessions.session_id` |
| `parent_id` | TEXT | 是 | 父行 `entry_id`；首条为 `NULL` |
| `entry_type` | TEXT | 否 | `message` / `compaction` / `openviking` / `model_change` |
| `payload_json` | TEXT | 否 | 该类型正文的 JSON 对象 |
| `created_at` | TEXT | 否 | UTC ISO；写入时生成，之后不改 |

**主键**：仅 `entry_id`（全局唯一，便于 `leaf_id` 单列引用）  

**索引**：

- `(session_id, parent_id)` — 查子节点 / 分支  
- `(session_id, created_at)` — 调试列举、同父兄弟次序辅助  

### 4.1 Entry 只追加

- 只 **INSERT**，禁止 UPDATE `payload_json` / `parent_id` / `entry_type` / `created_at`  
- 纠错、重说、换模型：靠 **新 entry + 改 `leaf_id`**，不改历史行  

### 4.2 默认写入：严格链式 append

**不变量**：正常对话推进时，每条新 entry 的 `parent_id = 当前 leaf_id`（首条为 `NULL`），写完后 `leaf_id = 新 entry_id`。

因此活动路径是一条 **链表**，不是「同 step 多子扇出」：

```text
user
 └─ assistant(tool_calls=[c1,c2])
      └─ tool(c1)
           └─ tool(c2)
                └─ user / assistant / …
```

| 现象 | 含义 |
|------|------|
| 同 `parent_id` 下多个子节点 | **分支**（曾从该点 fork），不是并行 tool 结果 |
| 同 step 多个 tool 结果 | **链式**依次 append：`assistant → tool(c1) → tool(c2)` |
| 兄弟次序 | 仅分支场景需要；按 `created_at` 升序（同毫秒则 `entry_id` 字典序） |

### 4.3 原子写（事务）

下列操作必须在 **同一 SQLite 事务** 内成功或全部回滚：

1. `INSERT` 一条（或同一次 flush 的多条）`session_entries`  
2. `UPDATE sessions SET leaf_id = 最后一条.entry_id, updated_at = ?`  

失败不得留下「有 entry 但 leaf 未动」或「leaf 指向不存在 entry」。  
切分支：仅 `UPDATE sessions.leaf_id`（+ `updated_at`），不写 entry；也须校验目标 entry 属于本 `session_id`。

### 4.4 删除

| 操作 | 行为 |
|------|------|
| 删除会话 | 先 `DELETE FROM session_entries WHERE session_id = ?`，再 `DELETE FROM sessions WHERE session_id = ?`（同事务） |
| 删除单条 entry | **不允许**（append-only） |
| 归档 | 只改 `sessions.status = archived`，不删行 |

---

## 5. 读路径：从库还原「当前对话」

### 5.1 加载当前路径（主路径）

业务默认需要的是 **leaf 回溯路径**，不是整棵树的 DFS。

```text
算法 load_active_path(session_id):

  1. 读 sessions 行；若不存在 → None
  2. 若 leaf_id 为空 → 路径 = []
  3. 读该 session 全部 entries，建 map: entry_id → row
     （会话规模可控时一次加载即可；日后可再优化）
  4. cur = leaf_id；path = []
     while cur is not None:
       row = map[cur]；若不存在或 session_id 不匹配 → 视为损坏，中止/报错
       path.append(row)
       cur = row.parent_id
  5. path.reverse()  →  根 → … → leaf
  6. 返回 Session 封面 + path
```

### 5.2 投影提示（落库层只定义事实，不实现模型 API）

| entry_type | 默认是否进入「发给模型的消息列表」 |
|------------|-------------------------------------|
| `message` | 是（按 role 解释） |
| `compaction` | 是：用其 `summary` 替代路径上更早段落的展开方式见 §6.2；**不删库内原文** |
| `openviking` | **否**（集成元数据） |
| `model_change` | **否**（或仅影响后续请求的模型选择，不进 messages 数组） |

完整 Model Context 组装不在本文。

### 5.3 列举分支（可选能力）

对路径上某节点 `E`：查询 `parent_id = E.entry_id` 的所有子节点；多于 1 个即存在分支。  
当前活动子节点 = 活动路径上 `E` 的后继（若有）。

---

## 6. `entry_type` 与 `payload_json` 形状

`payload_json` 反序列化后的字段如下。  
**都是库里的数据**，不是运行时对象。

### 6.1 `entry_type = message`

对话一行。

| 字段 | 类型 | 何时有 | 说明 |
|------|------|--------|------|
| `role` | string | 总是 | `user` / `assistant` / `tool` |
| `content` | string | 总是 | 文本；无正文时用 `""` |
| `tool_calls` | array \| null | assistant | 工具**调用**列表；无则 `null` 或省略 |
| `tool_call_id` | string \| null | tool | 对应某个 call 的 id；tool **必填** |
| `is_error` | bool \| null | tool | 工具是否失败；默认可视作 `false` |
| `tool_name` | string \| null | tool | 可选，展示用 |
| `metadata` | object \| null | assistant | 用量等，见下 |
| `provider_thinking_blocks` | array \| null | assistant | 思考块**透传** provider 原结构，本设计不展开元素 schema |

**应用层校验（写入前，非 DB 约束）**：

| role | 要求 |
|------|------|
| `user` | 无 `tool_calls` / `tool_call_id` |
| `assistant` | 可有 `tool_calls`、`metadata`、`provider_thinking_blocks` |
| `tool` | 必须有非空 `tool_call_id`；无 `tool_calls` |

**`tool_calls[]` 元素：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 调用 id |
| `name` | string | 工具名 |
| `arguments` | object | 参数 |
| `thought_signature` | string \| null | 可选；二进制用 base64 字符串入库 |

**`metadata` 对象（assistant）：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `provider` | string | 提供商 |
| `model` | string | 模型名 |
| `input_tokens` | int \| null | |
| `output_tokens` | int \| null | |
| `total_tokens` | int \| null | |
| `elapsed_ms` | int \| null | |
| `provider_finish_reason` | string \| null | |
| `provider_finish_message` | string \| null | |
| `provider_response_id` | string \| null | |
| `provider_model_version` | string \| null | |

**三种 message 示例：**

```json
// user
{"role":"user","content":"读 a.py 和 b.py"}

// assistant 带两个 tool 调用（还没有结果）
{
  "role": "assistant",
  "content": "",
  "tool_calls": [
    {"id":"c1","name":"read_file","arguments":{"path":"a.py"}},
    {"id":"c2","name":"read_file","arguments":{"path":"b.py"}}
  ],
  "metadata": {"provider":"anthropic","model":"claude-..."}
}

// tool 结果（一条 entry 一个结果）
{"role":"tool","tool_call_id":"c1","content":"...","is_error":false,"tool_name":"read_file"}
```

**同一 step 在库中的链：**

```text
assistant(tool_calls) → tool(c1) → tool(c2)
```

**没有** `tool_call_batch` 字段（列与 JSON 均无）。

#### 相对旧内存模型：字段去留

旧实现把一批 tool 塞在 `SessionMessage.tool_call_batch` 内。新库拆开后：

| 旧字段 | 新落库 |
|--------|--------|
| `role` / `content` | 同 |
| `metadata` | assistant.`metadata` |
| `provider_thinking_blocks` | 同 |
| `tool_call_batch.calls[]` | assistant.`tool_calls` |
| `tool_call_batch.results[]` | 各一条 `role=tool` entry |
| `tool_call_batch.batch_id` | **故意不保留** |
| `tool_call_batch.step_index` | **故意不保留**（次序由链与 `created_at` 表达） |
| `ToolCallResult.metadata` | **故意不保留**（需要时可日后加 tool payload 可选字段，v1 不做） |

（再次强调：升级**不**写旧库自动迁移脚本；上表只约束**新代码写入形状**与领域模型演进。）

---

### 6.2 `entry_type = compaction`

路径上的压缩标记；**不删除**被摘要掉的历史 entry。

| 字段 | 类型 | 说明 |
|------|------|------|
| `summary` | string | 摘要正文 |
| `first_kept_entry_id` | string | 从该 entry 起（含）路径上仍按原文展开；须为本 session 内、且位于**插入点之前的活动路径祖先链**上 |
| `tokens_before` | int \| null | 可选，压缩前估算 |
| `details` | object \| null | 可选，调试/策略名等 |

**落库合同（投影另文可细化，库层先钉死这些）：**

1. compaction 与其它 entry 一样 **链式 append** 到当时 leaf（或实现选择插在路径某点后：仍遵守 parent/leaf 规则）。  
2. 库内保留 `first_kept` 之前的原文 entry，便于 UI 展开或调试。  
3. 多条 compaction：投影时沿活动路径从 leaf 向根扫，**以较新（靠近 leaf）的为准**覆盖更早策略，或实现约定「只认路径上最后一条」——推荐 **只认活动路径上最后一条 compaction**。  
4. 无效 `first_kept_entry_id`（不存在 / 跨 session / 不在祖先链）视为损坏数据，加载可告警并降级为忽略该 compaction。

---

### 6.3 `entry_type = openviking`（扩展位，非会话封面字段）

OpenViking **相关事实的扩展落点**。  
**不要**做成 `sessions` 上的 `openviking_*` / `remote_session_id` / 同步 index 列。

| 字段 | 类型 | 说明 |
|------|------|------|
| `kind` | string | 区分用途，例如 `binding` / `sync_cursor`；开放小写标识，接入时再固化枚举 |
| `remote_session_id` | string \| null | 远程会话 id |
| `account_id` | string \| null | 远程账号 |
| `user_id` | string \| null | 远程用户 |
| `agent_id` | string \| null | **远程** agent 标识，≠ `sessions.agent_id` |
| `data` | object \| null | 其余键值（游标、commit 时间等塞这里即可） |

**策略：**

- v1 schema **预留类型与字段**，不强制写入；无 openviking 时会话可完全没有此类 entry  
- **不迁移**旧库上的 openviking 列与 index；新会话按需写入  
- 默认投影 **不进** 模型 messages  
- 同一 session 可有多条（不同 `kind` / 时间线）；读取「当前绑定」时由接入层约定（例如路径上最后一条 `kind=binding`），**不在本文定同步状态机**

---

### 6.4 `entry_type = model_change`

用户或系统切换本会话后续默认模型时写入（可选功能；类型预留，runtime 可不写）。

| 字段 | 类型 | 说明 |
|------|------|------|
| `provider` | string | 提供商 |
| `model_id` | string | 模型名 |

不进入 messages 数组；若实现「按历史还原当时模型」，可沿路径找 leaf 之前最近一条。

---

## 7. 与旧库对照（认知用，非迁移脚本）

### 旧表（现状）

- `sessions`：含 `last_synced_message_index`、`openviking_*`、`remote_session_id` 等  
- `session_messages`：`(session_id, message_index, payload_json)` 线性  

### 新表（目标）

| 旧 | 新 |
|----|-----|
| `sessions.session_id` | 同名 |
| `sessions.agent_id` | 同名，**保留且创建后不可变** |
| 线性 `message_index` | `entry_id` + `parent_id` 链/树 + `leaf_id` |
| payload 内 `SessionMessage` | `entry_type=message`（Pi 式 role） |
| `tool_call_batch` | assistant.`tool_calls` + 多条 tool message；batch 元数据丢弃 |
| session 上 openviking / 同步列 | **去掉**；需要时用 `entry_type=openviking` 扩展，**不兼容搬迁** |
| 无分支 / 无 compaction | 树 + `compaction` / `model_change` 类型 |

**兼容结论**：不提供自动 upgrade path；新版本使用新库文件或清空重建。

---

## 8. 建表 SQL 示意

```sql
PRAGMA user_version = 2;

CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    leaf_id      TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    status       TEXT NOT NULL,
    title        TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_agent_updated
    ON sessions (agent_id, updated_at);

CREATE TABLE IF NOT EXISTS session_entries (
    entry_id     TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL,
    parent_id    TEXT,
    entry_type   TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_entries_session_parent
    ON session_entries (session_id, parent_id);

CREATE INDEX IF NOT EXISTS idx_entries_session_created
    ON session_entries (session_id, created_at);
```

说明：`leaf_id` / `parent_id` 不设 DB 级 FK，避免 SQLite 下与「先插 entry 再改 leaf」顺序、以及历史调试导入纠缠；由 repository 校验。

---

## 9. 不变量（落库层清单）

1. 一场对话 = 一行 `sessions` + 零或多行 `session_entries`。  
2. `sessions.agent_id` 必填，指向配置中的 Agent；创建后不改。  
3. `entry_id` 全局唯一；`leaf_id` 为空表示尚无内容。  
4. Entry **只追加**；分支 = 同父多子 + 改 `leaf_id`；禁止改历史 payload。  
5. 默认推进为 **严格链式** append；同父多子 **只表示分支**，不表示并行 tool。  
6. 多 tool：一条 assistant message + N 条 tool message，用 `tool_call_id` 配对；链上顺序 `assistant → tool…`。  
7. 无 `tool_call_batch` 列或 JSON 字段；不保留 `batch_id` / `step_index`。  
8. append 与更新 `leaf_id`/`updated_at` **同事务**。  
9. 删会话 = 先 entries 后 sessions，同事务；不单删 entry。  
10. openviking / 模型切换等集成态用 entry 扩展，**不**堆回 `sessions` 列。  
11. 时间一律 UTC ISO8601；`status` ∈ {`active`,`archived`}。  

---

## 10. 一句话

> **库只存两样：Session 封面（agent_id、leaf_id、状态标题），和 Entry 链/树（message / compaction / openviking / model_change）。**  
> **读当前对话 = 从 leaf 回溯 parent；写 = 链式 append + 事务更新 leaf。**  
> **其余全是运行时；旧库不兼容迁移，openviking 只留扩展位。**

---

## 修订记录

| 日期 | 说明 |
|------|------|
| 2026-07-12 | 初版：仅数据库实体与表结构 |
| 2026-07-12 | 增强：全局约定、可 UPDATE 字段、链式/分支不变量、事务与删除、leaf 回溯算法、compaction 合同、字段去留、openviking 仅扩展不兼容、SQL `user_version` |
