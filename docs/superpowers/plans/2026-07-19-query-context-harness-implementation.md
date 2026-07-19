# Query → Context → Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按升级设计把 Pickel 从线性 `Session.messages` + `GenerateRequest` 拼装，迁移为 Session/Entry 持久事实、统一 `AgentMessage`、唯一 `ModelContext` 组装路径、分 checkpoint 的 ReAct，以及生命周期 Hook 扩展点。

**Architecture:** SQLite 只保存 `Session` + append-only `SessionEntry` 树（`leaf_id` 指向活动路径末端）。message entry 的 payload 是版本化 `AgentMessage`（role-specific content blocks）。`ContextAssembler` 无状态地从 active path + system + tools + 已生效运行时结果生成唯一 `ModelContext`。Provider 消费 `ModelContext` 并返回 `AssistantMessage`。`AgentCoordinator`/`ReActStrategy` 在固定 checkpoint 落盘，并在固定迁移点调用 `LifecycleHooks`。旧库不做自动迁移（`PRAGMA user_version = 2`，空库/新库为准）。OpenViking 与插件持久化放到最后一阶段。

**Tech Stack:** Python 3.12、dataclasses、sqlite3、asyncio、Typer/Rich CLI、unittest/pytest via `uv run`、现有 Anthropic/Gemini Provider

**Spec:** `docs/upgrade/2026-07-12-query-context-harness.md`  
**DB Spec:** `docs/upgrade/2026-07-12-db-entities.md`（payload 形状以 harness 的 content blocks 为准；db-entities 中 `content: string` 视为过时，实现时以本计划与 harness 为准）  
**As-Is 参考:** `docs/superpowers/specs/2026-07-09-model-context-request-pipeline-p0.md`

---

## 实现期默认决议（与设计 §1.2 同步；禁止实现期再发明同义类型）

| 议题 | 第一版决议 |
|------|------------|
| `model_change` entry | **不实现写入**；类型字符串若出现则投影跳过 |
| `SystemContent` | `SystemContent(sections: list[SystemSection])`，`SystemSection(name: str, text: str)`；`from_text` / `as_text`；section **克制** |
| User document | **不做**；仅 `TextContent` + `ImageContent` |
| Thinking | `ThinkingContent(text: str, signature: str \| None = None)`；**无 `opaque` 字段** |
| 持久 tool call 主名 | **`ToolCallContent`**；迁移窗口可 re-export 旧 `ToolCall`，Task 8 前删除并列领域类型 |
| Hook 反馈 | 类型名 **`HookFeedback`**（非 HookModelFeedback）；`TurnState.hook_feedback: list[HookFeedback]`；Assembler **只注入本 step 新增**项，作 ModelContext **尾部合成 user 文本**（不落库）；`source_event` 仅观测 |
| `PostToolUse` 替换结果 | **不允许**；仅观察 + 下一步 `HookFeedback` |
| compaction 投影 | 活动路径上 **最后一条** compaction：`summary` → `UserMessage`（前缀 `[compaction]`），丢弃 `first_kept` 之前展开 |
| `/context` 首次调用前 | 提示「尚无实际 ModelContext」；预测组装标 `predicted=true`；**不触发 Hook** |
| 并行 tool 落盘 | 工具可并行执行；**单一提交器**按 **调用顺序** 串行 `append_tool_result` |
| OpenViking Session 字段 | P1 从核心 Session **删除**；相关测试 noop/skip；P9 旁路表（推荐）或旁路 entry |
| Session `status` | 仅 `active` \| `archived`；`close` → `archived` |
| `message_count` | **active_path 上 message entry 数**（不含 compaction） |
| PreToolUse deny/ask | 合成 `ToolResultMessage(is_error=True)`；ask 无 UI 时按 deny |
| BeforeCompact/AfterCompact | **P8**；P6 不含 |
| SessionStart/SessionEnd | **第一版不做** |
| `HookFeedback` 位置 | `context/hook_feedback.py`；`TurnState` 只引用 |
| 运行时工具结果名 | **仅** `ToolExecutionOutcome`（删除长期 `ToolCallOutcome`） |
| 依赖容器 | **仅** `RunDependencies`；工具环境用具体字段，**不**建 `ExecutionEnvironment` 空壳类 |
| Turn/Step | **瘦字段**；禁止拷贝全量 messages / 开放 dict bag |
| Generate 共享 DTO | 领域层删除 `GenerateRequest`/`GenerateResult`；Provider 边界 `ModelContext`→`AssistantMessage`；usage 差分若暂需 thin wire DTO 仅限 Provider/内部，不得回流 conversations |

### 原则红线（执行时违反即回退）

1. 禁止第二套 ModelContext 拼装（ReAct 与 `/context` 共用 Assembler）。  
2. 禁止 tool result 与 assistant tool call 再落成同一条持久消息。  
3. 禁止 Session 核心挂 OV/sync 游标字段。  
4. 禁止 `ToolDefinition` 与 `ToolSpec` 合成一类。  
5. 禁止 Hook 直接改 Session 内部或 `Hook(ModelContext)->ModelContext`。  
6. 禁止迁移窗口外保留 `SessionMessage` / 落盘 `ToolCallBatch` 作为合同。  
7. 注释/错误中文；类型名与路径英文；`Harness` 不作代码包名。

---

## File Structure

### 新建

| 路径 | 职责 |
|------|------|
| `src/myopenclaw/conversations/agent_message.py` | `AgentMessage` 联合类型、`ModelResponseMetadata`/`ModelUsage` |
| `src/myopenclaw/conversations/session_entry.py` | `SessionEntry`、entry_type 常量、payload 版本、序列化 |
| `src/myopenclaw/conversations/content_blocks.py` | `TextContent`/`ImageContent`/`ThinkingContent`/`ToolCallContent` 与 JSON codec |
| `src/myopenclaw/context/model_context.py` | `ModelContext`、`SystemContent`、`SystemSection`、`ToolDefinition` |
| `src/myopenclaw/context/hook_feedback.py` | **`HookFeedback`**（Assembler 可消费；不依赖 hooks/runs） |
| `src/myopenclaw/context/assembler.py` | `ContextAssembler`：投影 / compaction / window / 组装 |
| `src/myopenclaw/context/projection.py` | active path → `list[AgentMessage]`（含 compaction 规则） |
| `src/myopenclaw/context/window.py` | 不可拆分单元分组 + 窗口裁剪 |
| `src/myopenclaw/runs/dependencies.py` | `RunDependencies`（替代 `AgentRuntimeContext`） |
| `src/myopenclaw/runs/turn_state.py` | 瘦 `TurnState` / `StepState` / `ToolExecutionOutcome`；`hook_feedback` 引用 `HookFeedback` |
| `src/myopenclaw/hooks/__init__.py` | 包导出 |
| `src/myopenclaw/hooks/events.py` | Hook 事件 DTO |
| `src/myopenclaw/hooks/decisions.py` | Hook 决策 DTO 与合并规则 |
| `src/myopenclaw/hooks/lifecycle.py` | `LifecycleHooks` 分发器 |
| `tests/conversations/test_agent_message.py` | 消息序列化 round-trip |
| `tests/conversations/test_session_entry_tree.py` | Session 树 / active_path / append 不变量 |
| `tests/persistence/test_sqlite_session_entries.py` | 新 schema 持久化 |
| `tests/context/test_assembler.py` | 唯一 ModelContext 路径 |
| `tests/context/test_projection.py` | compaction / message 投影 |
| `tests/context/test_window.py` | tool 原子组不拆分 |
| `tests/hooks/test_lifecycle_hooks.py` | 合并与失败策略 |
| `tests/runs/test_react_checkpoint.py` | 工具前 intent 落盘 |
| `tests/providers/test_model_context_generate.py` | Provider 消费 ModelContext（含多轮 tool history） |

### 重写 / 大改

| 路径 | 变化 |
|------|------|
| `src/myopenclaw/conversations/session.py` | `messages` → `entries` + `leaf_id`；`append_user/assistant/tool_result`；`active_path()`；移除 OpenViking 字段 |
| `src/myopenclaw/conversations/repository.py` | `append_entry` / `load` 基于 entries；去掉 `append_messages` |
| `src/myopenclaw/conversations/service.py` | flush/checkpoint API 对齐 entry |
| `src/myopenclaw/conversations/session_storage_mapper.py` | entry payload codec；删除 batch 形状 |
| `src/myopenclaw/persistence/sqlite_session_repository.py` | `sessions` + `session_entries`，`user_version=2` |
| `src/myopenclaw/context/service.py` | 删除或退化为薄封装；组装迁入 Assembler |
| `src/myopenclaw/runs/context.py` | 迁移为 `RunDependencies` 兼容层或直接替换 |
| `src/myopenclaw/runs/strategy/react.py` | ModelContext + checkpoint 落盘 + 可选 Hooks |
| `src/myopenclaw/runs/coordinator.py` | turn 边界 + UserPromptSubmit/TurnEnd |
| `src/myopenclaw/providers/base.py` | `generate(context: ModelContext) -> AssistantMessage` |
| `src/myopenclaw/providers/anthropic.py` | 适配 ModelContext / AssistantMessage |
| `src/myopenclaw/providers/gemini.py` | 同上 |
| `src/myopenclaw/shared/generation.py` | 删除或内部化 `GenerateRequest`/`GenerateResult` |
| `src/myopenclaw/cli/context_renderer.py` | 展示 final ModelContext + cache usage |
| `src/myopenclaw/cli/chat.py` | 依赖 SessionService checkpoint；去掉 batch 渲染假设 |
| `src/myopenclaw/app/assembly.py` | 装配 Assembler / Hooks / 新 SessionService |
| OpenViking 相关文件 | P1–P8 **停用核心耦合**；P9 旁路重接 |

### 保留暂不删除（迁移窗口，Task 8 结束前必须收口）

- `conversations/message.py`：`ToolCall` 可 re-export → `ToolCallContent`；`SessionMessage`/`ToolCallBatch` **不得**再作为落盘合同
- Task 5–8 允许临时 shim，但 **禁止** ReAct 与 `/context` 长期双路径拼装 ModelContext
- CLI 渲染游标：从 `message index` 迁到 **entry_id / 已持久 entry 集合**；每个 checkpoint 后 flush

---

## Task 1: P0 — Content Blocks 与 AgentMessage 合同

**Files:**
- Create: `src/myopenclaw/conversations/content_blocks.py`
- Create: `src/myopenclaw/conversations/agent_message.py`
- Create: `tests/conversations/test_agent_message.py`

- [ ] **Step 1: 写失败测试（序列化 round-trip）**

```python
# tests/conversations/test_agent_message.py
from myopenclaw.conversations.agent_message import (
    AssistantMessage,
    ModelResponseMetadata,
    ModelUsage,
    ToolResultMessage,
    UserMessage,
    agent_message_from_dict,
    agent_message_to_dict,
)
from myopenclaw.conversations.content_blocks import (
    TextContent,
    ThinkingContent,
    ToolCallContent,
)


def test_user_message_round_trip():
    msg = UserMessage(content=[TextContent(text="hello")])
    restored = agent_message_from_dict(agent_message_to_dict(msg))
    assert restored == msg


def test_assistant_with_thinking_and_tool_calls_round_trip():
    msg = AssistantMessage(
        content=[
            ThinkingContent(text="plan", signature="sig"),
            TextContent(text="calling tools"),
            ToolCallContent(id="c1", name="read_file", arguments={"path": "a.py"}),
        ],
        metadata=ModelResponseMetadata(
            provider="anthropic",
            model="claude-test",
            usage=ModelUsage(
                input_tokens=10,
                output_tokens=5,
                cache_read_tokens=3,
                cache_write_tokens=1,
            ),
        ),
    )
    restored = agent_message_from_dict(agent_message_to_dict(msg))
    assert restored == msg


def test_tool_result_message_round_trip():
    msg = ToolResultMessage(
        tool_call_id="c1",
        tool_name="read_file",
        content=[TextContent(text="ok")],
        is_error=False,
    )
    restored = agent_message_from_dict(agent_message_to_dict(msg))
    assert restored == msg
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/conversations/test_agent_message.py -v`  
Expected: FAIL import / not found

- [ ] **Step 3: 实现 content blocks 与 AgentMessage**

实现要点：

```python
# content_blocks.py — frozen dataclass
# TextContent(type="text", text: str)
# ImageContent(type="image", media_type: str, data_base64: str | None = None, url: str | None = None)
# ThinkingContent(type="thinking", text: str, signature: str | None = None)  # 无 opaque
# ToolCallContent(type="tool_call", id: str, name: str, arguments: dict, thought_signature: str | None = None)
# 二进制 thought_signature 若来自 bytes：codec 层 base64

# agent_message.py
# PAYLOAD_VERSION = 1
# UserMessage(role="user", content: list[UserContent])
# AssistantMessage(role="assistant", content: list[AssistantContent], metadata: ModelResponseMetadata | None)
# ToolResultMessage(role="tool", tool_call_id, tool_name, content: list[ToolResultContent], is_error: bool)
# AgentMessage = UserMessage | AssistantMessage | ToolResultMessage
# agent_message_to_dict / agent_message_from_dict 顶层带 "payload_version": 1 与 "role"
```

`ModelUsage` 字段：`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `total_tokens`（均可 `int | None`）。

`ModelResponseMetadata`：`provider`, `model`, `provider_model_version`, `provider_response_id`, `finish_reason`, `finish_message`, `elapsed_ms`, `usage`。

- [ ] **Step 4: 测试通过**

Run: `uv run pytest tests/conversations/test_agent_message.py -v`  
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/myopenclaw/conversations/content_blocks.py \
  src/myopenclaw/conversations/agent_message.py \
  tests/conversations/test_agent_message.py
git commit -m "feat(p0): 增加 AgentMessage 与 content blocks 序列化合同"
```

---

## Task 2: P0 — ModelContext 与 ToolDefinition

**Files:**
- Create: `src/myopenclaw/context/model_context.py`
- Create: `tests/context/test_model_context.py`

- [ ] **Step 1: 写失败测试**

```python
from myopenclaw.context.model_context import (
    ModelContext,
    SystemContent,
    SystemSection,
    ToolDefinition,
)
from myopenclaw.conversations.agent_message import UserMessage
from myopenclaw.conversations.content_blocks import TextContent


def test_model_context_holds_system_messages_tools():
    ctx = ModelContext(
        system=SystemContent(sections=[SystemSection(name="behavior", text="you are pickle")]),
        messages=[UserMessage(content=[TextContent(text="hi")])],
        tools=[ToolDefinition(name="read_file", description="read", input_schema={"type": "object"})],
    )
    assert ctx.system.sections[0].name == "behavior"
    assert len(ctx.messages) == 1
    assert ctx.tools[0].name == "read_file"
```

- [ ] **Step 2: 运行确认失败**

Run: `uv run pytest tests/context/test_model_context.py -v`  
Expected: FAIL

- [ ] **Step 3: 实现 ModelContext**

```python
@dataclass(frozen=True)
class SystemSection:
    name: str
    text: str

@dataclass(frozen=True)
class SystemContent:
    sections: list[SystemSection] = field(default_factory=list)

    @classmethod
    def from_text(cls, text: str) -> "SystemContent":
        if not text:
            return cls(sections=[])
        return cls(sections=[SystemSection(name="system", text=text)])

    def as_text(self) -> str:
        return "\n\n".join(section.text for section in self.sections if section.text)

@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]

@dataclass(frozen=True)
class ModelContext:
    system: SystemContent
    messages: list[AgentMessage]
    tools: list[ToolDefinition] = field(default_factory=list)
```

注意：`ToolDefinition` 与 `tools.base.ToolSpec` 字段对齐但类型分离；后续 Assembler 从 `ToolSpec` 映射。

- [ ] **Step 4: 测试通过并 Commit**

```bash
git add src/myopenclaw/context/model_context.py tests/context/test_model_context.py
git commit -m "feat(p0): 增加 ModelContext 与 ToolDefinition 值对象"
```

---

## Task 3: P1 — SessionEntry 与内存 Session 树

**Files:**
- Create: `src/myopenclaw/conversations/session_entry.py`
- Modify: `src/myopenclaw/conversations/session.py`
- Create: `tests/conversations/test_session_entry_tree.py`
- Modify: `tests/conversations/test_session.py`（改写为 entry API）

- [ ] **Step 1: 写失败测试（active_path 与链式 append）**

```python
def test_append_chain_and_active_path():
    session = Session.create(agent_id="Pickle")
    u = session.append_user(UserMessage(content=[TextContent(text="hi")]))
    a = session.append_assistant(
        AssistantMessage(content=[ToolCallContent(id="c1", name="t", arguments={})])
    )
    t = session.append_tool_result(
        ToolResultMessage(tool_call_id="c1", tool_name="t", content=[TextContent(text="ok")])
    )
    path = session.active_path()
    assert [e.entry_id for e in path] == [u.entry_id, a.entry_id, t.entry_id]
    assert session.leaf_id == t.entry_id
    assert path[0].parent_id is None
    assert path[1].parent_id == u.entry_id


def test_entries_are_append_only():
    session = Session.create(agent_id="Pickle")
    e = session.append_user(UserMessage(content=[TextContent(text="x")]))
    # 不应提供 mutate payload 的公开 API；payload 为不可变结构
    assert e.entry_type == "message"
```

- [ ] **Step 2: 实现 SessionEntry 与 Session 行为**

```python
# session_entry.py
ENTRY_TYPE_MESSAGE = "message"
ENTRY_TYPE_COMPACTION = "compaction"
MESSAGE_PAYLOAD_VERSION = 1
COMPACTION_PAYLOAD_VERSION = 1

@dataclass(frozen=True)
class SessionEntry:
    entry_id: str
    session_id: str
    parent_id: str | None
    entry_type: str
    payload: dict[str, Any]  # 已版本化 JSON-ready dict
    created_at: datetime

# session.py
@dataclass
class Session:
    session_id: str
    agent_id: str
    leaf_id: str | None = None
    entries: list[SessionEntry] = field(default_factory=list)
    created_at: datetime = ...
    updated_at: datetime = ...
    status: str = "active"
    title: str | None = None
    # 删除: messages, remote_session_id, openviking_*, last_synced_*, bind_openviking, mark_messages_*

    def active_path(self) -> list[SessionEntry]: ...
    def append_user(self, message: UserMessage) -> SessionEntry: ...
    def append_assistant(self, message: AssistantMessage) -> SessionEntry: ...
    def append_tool_result(self, message: ToolResultMessage) -> SessionEntry: ...
    def append_compaction(self, payload: dict) -> SessionEntry: ...
    def move_leaf(self, entry_id: str) -> None: ...
```

不变量：

1. append 时 `parent_id = leaf_id`（首条 `None`）  
2. 更新 `leaf_id` 与 `updated_at`  
3. 禁止原地改 `entries[i].payload`  
4. `move_leaf` 必须指向本 session 已有 entry

- [ ] **Step 3: 修复 `test_session.py` 等直接依赖 `messages` 的测试**  
  本 Task 内先让 **新建测试** 全绿；旧测试若大面积失败，可暂时 `pytest` 过滤到新文件，但 **Task 4 结束前** 必须让 `tests/conversations/` 与 `tests/persistence/` 全绿。

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(p1): Session 改为 entry 树与 active_path"
```

---

## Task 4: P1 — SQLite sessions + session_entries

**Files:**
- Modify: `src/myopenclaw/conversations/repository.py`
- Modify: `src/myopenclaw/conversations/session_storage_mapper.py`
- Modify: `src/myopenclaw/conversations/service.py`
- Modify: `src/myopenclaw/persistence/sqlite_session_repository.py`
- Create: `tests/persistence/test_sqlite_session_entries.py`
- Modify: `tests/persistence/test_sqlite_session_repository.py`
- Modify: `tests/conversations/test_session_service.py`
- Modify: `tests/conversations/test_session_storage_mapper.py`

- [ ] **Step 1: 写失败测试（事务 append + 恢复 active path）**

```python
def test_create_append_reload_active_path(tmp_path):
    repo = SQLiteSessionRepository(tmp_path / "s.db")
    session = Session.create(agent_id="Pickle")
    repo.create(session)
    session.append_user(UserMessage(content=[TextContent(text="hi")]))
    repo.append_entries(
        session_id=session.session_id,
        entries=session.entries[-1:],
        leaf_id=session.leaf_id,
        updated_at=session.updated_at,
    )
    loaded = repo.load(session.session_id)
    assert loaded is not None
    assert len(loaded.active_path()) == 1
    assert loaded.leaf_id == session.leaf_id


def test_append_entry_and_leaf_are_atomic(tmp_path, monkeypatch):
    repo = SQLiteSessionRepository(tmp_path / "s.db")
    session = Session.create(agent_id="Pickle")
    repo.create(session)
    session.append_user(UserMessage(content=[TextContent(text="hi")]))
    entry = session.entries[-1]

    real_connect = repo._connect

    class BoomConnection:
        def __init__(self, inner):
            self._inner = inner
        def __enter__(self):
            self._inner.__enter__()
            return self
        def __exit__(self, *args):
            return self._inner.__exit__(*args)
        def execute(self, sql, params=None):
            if sql.strip().upper().startswith("UPDATE") and "sessions" in sql.lower():
                raise sqlite3.OperationalError("simulated leaf update failure")
            if params is None:
                return self._inner.execute(sql)
            return self._inner.execute(sql, params)
        def __getattr__(self, name):
            return getattr(self._inner, name)

    def fake_connect():
        return BoomConnection(real_connect())

    monkeypatch.setattr(repo, "_connect", fake_connect)
    try:
        repo.append_entries(
            session_id=session.session_id,
            entries=[entry],
            leaf_id=entry.entry_id,
            updated_at=session.updated_at,
        )
        raised = False
    except Exception:
        raised = True
    assert raised

    monkeypatch.setattr(repo, "_connect", real_connect)
    loaded = repo.load(session.session_id)
    assert loaded is not None
    assert loaded.leaf_id is None
    assert loaded.entries == []
```

说明：若 `SQLiteSessionRepository` 用单 connection 上下文不同，可改为 patch 事务内 `UPDATE sessions` 的私有方法；**验收点不变**——失败后无半写入 entry、leaf 不变。

- [ ] **Step 2: 新 schema（不做旧库迁移）**

```sql
PRAGMA user_version = 2;

CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    leaf_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    title TEXT
);

CREATE TABLE session_entries (
    entry_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    parent_id TEXT,
    entry_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

CREATE INDEX idx_session_entries_session_parent
  ON session_entries(session_id, parent_id);
CREATE INDEX idx_session_entries_session_created
  ON session_entries(session_id, created_at);
CREATE INDEX idx_sessions_agent_updated
  ON sessions(agent_id, updated_at);
```

Repository API 建议：

```python
class SessionRepository(Protocol):
    def create(self, session: Session) -> None: ...
    def load(self, session_id: str) -> Session | None: ...
    def list(self, *, limit: int = 20) -> list[SessionPreview]: ...
    def append_entries(
        self,
        *,
        session_id: str,
        entries: list[SessionEntry],
        leaf_id: str | None,
        updated_at: datetime,
    ) -> None: ...
    def update_metadata(self, session: Session) -> None: ...  # title/status/leaf only
    def mark_closed(self, *, session_id: str, updated_at: datetime) -> None: ...
    def delete(self, *, session_id: str) -> None: ...
```

`append_entries`：**同一事务** INSERT entries + UPDATE sessions.leaf_id/updated_at。

- [ ] **Step 3: 改写 SessionService**

- `flush`：将未持久化 entries 通过 `append_entries` 写出（可用 `Session` 内存全量与 DB 对比，或维护 `persisted_entry_ids`；第一版允许 load 后内存即权威，每次 flush 传「自上次 flush 新增」列表）  
- `start` / `resume` / `close` / `delete` 对齐新字段  
- 删除对 OpenViking sync index 的 Session 依赖；`SessionSync` 调用点可保留但传入空 cursor 或暂时 noop（见 Task 12）

- [ ] **Step 4: 跑相关测试**

Run:

```bash
uv run pytest tests/persistence tests/conversations -v
```

Expected: PASS（OpenViking 测试若仍依赖旧 Session 字段，本 Task 内改为 skip 并注明 Task 12 恢复）

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(p1): SQLite sessions/session_entries 与原子 append"
```

---

## Task 5: P2 — 持久与展示层统一 AgentMessage（不含 generate 主路径）

**本 Task 边界（重要）：**

| 在范围内 | **不在**范围内（留给后续 Task） |
|----------|----------------------------------|
| Session/Entry payload、mapper、SessionService | `providers/*` 的 generate 签名（Task 7） |
| `session_preview`、event/CLI 展示字符串 | `runs/strategy/react.py` 主循环（Task 8） |
| 删除 **落盘** 形状中的 `ToolCallBatch` | 要求 `tests/runs` / 全量 CLI chat 本 Task 全绿 |
| `conversations/message.py` 降级或 re-export | 强行一次改完 Provider 解码 |

中间态允许：`tests/runs`、`tests/cli/test_chat_loop.py` **阶段性失败**，但须在 Task 8 结束前恢复。Task 5 验收以 `tests/conversations` + `tests/persistence` + preview 相关测试为准。

**Files:**
- Modify: `src/myopenclaw/conversations/message.py`（删除或降级 `SessionMessage`/`ToolCallBatch` 持久角色；`ToolCall` 可迁到 content_blocks 后 re-export）
- Modify: `src/myopenclaw/conversations/session_preview.py`、`session_storage_mapper.py`
- Modify: `src/myopenclaw/cli/event_renderer.py`（若只依赖消息展示）
- Modify: `tests/conversations/*`、`tests/persistence/*`

- [ ] **Step 1: 列出残留引用，区分「持久/展示」与「generate 主路径」**

Run: `rg -n "SessionMessage|ToolCallBatch|tool_call_batch" src tests`

- 对 `src/myopenclaw/providers/**`、`runs/strategy/react.py`：本 Task **只加 TODO 注释或最小 shim**，不重写 generate。  
- 对 conversations/persistence/preview：必须改为 `AgentMessage`。

- [ ] **Step 2: Preview / 展示渲染**

- assistant 含 `ToolCallContent` → preview `"[tools] name1,name2"`  
- tool result → 截断 text content  
- 删除 batch 专用分支

- [ ] **Step 3: 扩展序列化测试（可选 ImageContent）+ 跑验收**

Run: `uv run pytest tests/conversations tests/persistence -v`  
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(p2): 持久与展示层统一 AgentMessage，移除 batch 落盘"
```

---

## Task 6: P3 — 投影、窗口与 ContextAssembler

**Files:**
- Create: `src/myopenclaw/context/hook_feedback.py`
- Create: `src/myopenclaw/context/projection.py`
- Create: `src/myopenclaw/context/window.py`
- Create: `src/myopenclaw/context/assembler.py`
- Create: `tests/context/test_projection.py`
- Create: `tests/context/test_window.py`
- Create: `tests/context/test_assembler.py`
- Modify: `src/myopenclaw/context/service.py`（标记 deprecated 或改为委托 Assembler）

- [ ] **Step 0: 先定义 HookFeedback（供 Assembler 依赖，不依赖 hooks/runs）**

```python
# src/myopenclaw/context/hook_feedback.py
@dataclass(frozen=True)
class HookFeedback:
    source_event: str  # 如 "UserPromptSubmit" / "PostToolBatch"；仅观测
    text: str
```

- [ ] **Step 1: 投影测试**

```python
def test_project_messages_skips_unknown_entry_types():
    session = Session.create(agent_id="Pickle")
    session.append_user(UserMessage(content=[TextContent(text="hi")]))
    # 直接构造未知类型 entry 挂到树尾（测试辅助或 session 内部测试钩子）
    # project_messages(active_path) 只含 user，不含 openviking
    ...

def test_compaction_keeps_from_first_kept_and_injects_summary():
    # 构造 path: u1 -> a1 -> u2 -> a2 -> compaction(first_kept=u2.entry_id)
    # 投影结果: [UserMessage("[compaction]\\n"+summary), u2, a2]
    ...
```

- [ ] **Step 2: 窗口测试（不可拆分单元）**

单元定义：

1. 单独 `UserMessage`  
2. `AssistantMessage`（无 tool call）  
3. `AssistantMessage`(含 tool calls) + 紧随的对应 `ToolResultMessage` 序列（按 tool_call_id 匹配，直到下一条 user/assistant 最终回复）

窗口：保留最近 `N` 个 **单元**（配置键沿用 `cli_turn_window` 或改名 `context_unit_window`，默认 5），**禁止**只保留 assistant tool call 而丢掉 tool result。

- [ ] **Step 3: Assembler**

```python
class ContextAssembler:
    def assemble(
        self,
        *,
        entries: list[SessionEntry],  # active path
        system: SystemContent,
        tools: list[ToolDefinition],
        hook_feedback: list[HookFeedback] | None = None,
        unit_window: int = 5,
    ) -> ModelContext:
        messages = project_messages(entries)
        messages = apply_window(messages, unit_window=unit_window)
        # 调用方只传「本 step 新增」的 hook_feedback，避免多 step 累积重复注入
        messages = append_hook_feedback(messages, hook_feedback or [])
        return ModelContext(system=system, messages=messages, tools=list(tools))
```

**禁止依赖：** Repository、Provider、LifecycleHooks、OpenViking、CLI、AgentCoordinator。

system/tools **由调用方构造后传入**（Task 8：`SystemContent.from_text(...)` + `ToolSpec`→`ToolDefinition` 映射）。Assembler 内不读取 Agent。`ToolDefinition` 与 `ToolSpec` 不得合并为同一类型。

- [ ] **Step 4: 测试通过 + Commit**

```bash
git commit -m "feat(p3): ContextAssembler 唯一 ModelContext 组装路径"
```

---

## Task 7: P4 — Provider 消费 ModelContext，返回 AssistantMessage

**Files:**
- Modify: `src/myopenclaw/providers/base.py`
- Modify: `src/myopenclaw/providers/anthropic.py`
- Modify: `src/myopenclaw/providers/gemini.py`
- Modify: `src/myopenclaw/shared/generation.py`（删除 GenerateRequest 或仅内部使用）
- Modify: `tests/providers/test_anthropic.py`、`test_gemini.py`
- Create: `tests/providers/test_model_context_generate.py`

- [ ] **Step 1: 改协议**

```python
class BaseLLMProvider(ABC):
    @abstractmethod
    async def generate(self, context: ModelContext) -> AssistantMessage: ...

    async def count_context_tokens(self, context: ModelContext) -> int | None:
        return None
```

- [ ] **Step 2: Anthropic / Gemini adapter**

映射规则：

| ModelContext | Wire |
|--------------|------|
| `system.as_text()` | Anthropic `system` / Gemini system_instruction |
| `UserMessage` blocks | user content parts |
| `AssistantMessage` text/thinking/tool_call | assistant content |
| `ToolResultMessage` | tool_result / function response |
| `tools` | tool/function declarations |

返回：解码为 `AssistantMessage`（含 `ToolCallContent`、`ThinkingContent`、`ModelResponseMetadata.usage` 含 cache 字段若 Provider 提供）。

- [ ] **Step 3: 用现有 mock/fixture 改测试；补结构测试 + 多轮 tool history 黄金用例**

1. 同一 `ModelContext` 两种 provider 均接受（可不打真网）。  
2. **多轮 tool history**：`user → assistant(tool_calls) → tool_result×N → assistant(final)` 编解码往返（Anthropic：连续 tool_result 合成一条 user content；Gemini：function 响应 + thought_signature）。  

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(p4): Provider 直接消费 ModelContext 并返回 AssistantMessage"
```

---

## Task 8: P5 — ReAct 分 checkpoint 落盘

**Files:**
- Create: `src/myopenclaw/runs/dependencies.py`
- Create: `src/myopenclaw/runs/turn_state.py`
- Modify: `src/myopenclaw/runs/context.py`（兼容别名或删除）
- Modify: `src/myopenclaw/runs/strategy/react.py`
- Modify: `src/myopenclaw/runs/coordinator.py`
- Modify: `src/myopenclaw/cli/chat.py`、`app/assembly.py`
- Create: `tests/runs/test_react_checkpoint.py`
- Modify: `tests/runs/test_runner.py` 等

- [ ] **Step 1: 写 checkpoint 测试（fake provider + fake repo/session）**

场景：

1. user 输入后 session 先有 user entry  
2. provider 返回 tool calls → **执行工具前** session 已有 assistant entry（含 ToolCallContent）  
3. 每个 tool 完成后各有一条 tool result entry  
4. 最终 assistant 再 append  

可用 in-memory Session + 记录 `append_*` 顺序的 spy。

- [ ] **Step 2: ReAct 主循环伪代码**

```text
# 调用方准备 system/tools（唯一入口，禁止再走 ConversationContextService 拼 messages）
system = SystemContent.from_text(agent.system_instruction or "")
tools = [
    ToolDefinition(name=t.spec.name, description=t.spec.description, input_schema=t.spec.input_schema)
    for t in deps.tools
]
context = assembler.assemble(
    entries=session.active_path(),
    system=system,
    tools=tools,
    hook_feedback=turn_state.hook_feedback_for_current_step(),  # 仅本 step 新增
    unit_window=...,
)
assistant = await provider.generate(context)
session.append_assistant(assistant)   # checkpoint BEFORE tools
session_service.flush(...)

if has tool calls:
  run tools (parallel ok)
  for call in call_order:             # 串行提交
    session.append_tool_result(...)
    session_service.flush(...)
  continue next step
else:
  return assistant
```

崩溃边界：**工具副作用前 assistant intent 已在 Session 并 flush**。第一版 **每个 checkpoint 后 flush**。CLI 增量渲染用 entry 游标，避免 batch 双渲染。

- [ ] **Step 3: RunDependencies + 瘦 Turn/Step**

```python
@dataclass
class RunDependencies:
    agent: Agent
    provider: BaseLLMProvider
    tools: list[BaseTool]
    context_assembler: ContextAssembler
    lifecycle_hooks: LifecycleHooks  # Task 9 可先空 handlers
    session_service: SessionService
    # 具体字段：workspace_files / file_access_policy / shell_session_manager
    # 禁止空壳 ExecutionEnvironment 类型
```

```python
# TurnState 仅：
# turn_id, status, current_user_entry_id, current_step, hook_feedback, final_assistant_entry_id
# StepState 仅：
# step_index, status, assistant_entry_id, pending_tool_call_ids, completed_tool_call_ids
```

`TurnState.hook_feedback: list[HookFeedback]` **引用** `context.hook_feedback.HookFeedback`，runs 内不重复定义。运行时工具结果类型名 **仅** `ToolExecutionOutcome`。

删除 `conversation_context_service` / `session_recall_provider` 核心字段（recall 后置）。

- [ ] **Step 4: 全量 runs + cli 相关测试**

Run: `uv run pytest tests/runs tests/cli tests/app -v`

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(p5): ReAct 按 user/assistant/tool checkpoint 落盘"
```

---

## Task 9: P6 — Lifecycle Hooks（第一版事件）

**Files:**
- Create: `src/myopenclaw/hooks/events.py`
- Create: `src/myopenclaw/hooks/decisions.py`
- Create: `src/myopenclaw/hooks/lifecycle.py`
- Create: `tests/hooks/test_lifecycle_hooks.py`
- Modify: `react.py` / `coordinator.py` 接入

- [ ] **Step 1: 事件与决策类型**

第一版必须（**不含** BeforeCompact/AfterCompact，留给 Task 11）：

- `UserPromptSubmit` → continue/block + optional feedback text  
- `PreToolUse` → allow/deny/ask + optional updated_arguments  
- `PostToolUse` → feedback only（不可替换结果）  
- `PostToolBatch` → feedback only  
- `TurnEnd` → observer  

本阶段 **不做** `SessionStart` / `SessionEnd`（spec 允许按需逐步启用；需要会话级副作用时再开 Task）。

`ask` 第一版：合并后若最终为 `ask`，**按 deny 处理**，reason 固定为「需要确认（第一版未接 UI）」，避免半交互卡死。

- [ ] **Step 2: 合并规则 + deny 合成 tool result 测试**

```python
def test_user_prompt_any_block_wins(): ...
def test_pre_tool_deny_over_allow(): ...
def test_updated_arguments_applied_in_order_and_revalidated(): ...
def test_observer_failure_is_best_effort(): ...
def test_no_hooks_preserves_behavior(): ...

def test_pre_tool_deny_appends_synthetic_tool_result_before_next_model_step():
    """
    assistant 已 checkpoint 含 tool_call c1；
    PreToolUse deny 后必须 append ToolResultMessage(tool_call_id=c1, is_error=True)；
    不得执行真实 tool；随后 PostToolUse/PostToolBatch 仍可收到「结果已确定」视图。
    """
    ...
```

- [ ] **Step 3: LifecycleHooks API**

```python
class LifecycleHooks:
    def __init__(self, handlers: list[HookHandler] | None = None): ...
    async def user_prompt_submit(self, event: UserPromptSubmitEvent) -> UserPromptSubmitDecision: ...
    async def pre_tool_use(self, event: PreToolUseEvent) -> PreToolUseDecision: ...
    ...
```

默认 `handlers=[]` 时决策恒为 continue/allow。

- [ ] **Step 4: 接入状态机固定点（见 harness §11）**

固定行为：

1. `UserPromptSubmit` block → **不**写 user entry，turn 结束。  
2. allow 后写 user → 进入 model step。  
3. assistant（含 tool calls）先落盘。  
4. 对每个 tool call：`PreToolUse`  
   - **allow**（可带 updated_arguments，需再校验 schema）→ 执行工具 → `append_tool_result` 真实结果 → `PostToolUse`  
   - **deny / ask(按 deny)** → **不执行工具** → `append_tool_result` 合成错误结果（文本含 reason，`is_error=True`，`tool_call_id`/`tool_name` 对齐）→ `PostToolUse`  
5. 全部 call 处理完 → `PostToolBatch` → **本 step 新增** `HookFeedback` 写入 `TurnState.hook_feedback` → 下一 model step（Assembler 只吃本 step 列表）。  
6. 最终 assistant → `TurnEnd`。

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(p6): 生命周期 Hook 事件、合并规则与 ReAct 接入"
```

---

## Task 10: P7 — `/context` 观测 final ModelContext 与 cache usage

**Files:**
- Modify: `src/myopenclaw/runs/context_usage.py`
- Modify: `src/myopenclaw/cli/context_renderer.py`
- Modify: `src/myopenclaw/cli/chat.py`（保存 last ModelContext + last Assistant metadata）
- Modify: `tests/runs/test_context_usage.py`、cli 相关测试

- [ ] **Step 1: 定义观测快照**

```python
@dataclass
class ContextObservation:
    model_context: ModelContext | None
    predicted: bool
    assistant_metadata: ModelResponseMetadata | None
    # token breakdown: system/messages/tools/total if counter available
```

规则：

- **不**触发 Hook  
- **不**远程 recall  
- 优先 last real final ModelContext  
- 无则 `predicted=True` 用 Assembler 现算并提示  

- [ ] **Step 2: Renderer 展示 cache_read/write**

- [ ] **Step 3: Commit**

```bash
git commit -m "feat(p7): /context 展示 ModelContext 与 cache usage"
```

---

## Task 11: P8 — Compaction 完整行为

**Files:**
- Create: `src/myopenclaw/context/compaction.py`（策略：何时 compact、summary 文本）
- Modify: `projection.py`、`react.py`（Before model call 容量检查）
- Modify: `src/myopenclaw/hooks/events.py` / `lifecycle.py`（本 Task 增加 `BeforeCompact` / `AfterCompact`）
- Create: `tests/context/test_compaction.py`

- [ ] **Step 1: 合法切点**  
  只在 **单元边界** compact；不得切断 tool call/result 组。

- [ ] **Step 2: 写入 compaction entry**  
  payload：`summary`, `first_kept_entry_id`, `tokens_before?`, `details?`，`payload_version=1`。

- [ ] **Step 3: 多条 compaction 只认路径上最后一条**

- [ ] **Step 4: overflow：若仍超窗，允许再 compact 或截断更早单元（策略写在 details）**

- [ ] **Step 5: Hooks**  
  - `BeforeCompact`：continue / cancel / replace(summary)  
  - `AfterCompact`：observer  
  无 handler 时行为与无 Hook 相同。

- [ ] **Step 6: Commit**

```bash
git commit -m "feat(p8): compaction entry 与投影/触发完整行为"
```

---

## Task 12: P9 — OpenViking / 扩展旁路（最小可运行）

**Files:**
- Modify: `src/myopenclaw/integrations/openviking/*`
- Modify: 相关测试
- 可选: 旁路表或 `entry_type=openviking` payload（**不**回 Session 封面字段）

- [ ] **Step 1: 定义 OpenViking 状态存储**  
  优先：独立 sqlite 表 `integration_openviking_sessions(session_id PK, payload_json)` **或** session_entries 中 `entry_type=openviking`（投影跳过）。二选一写进实现注释；推荐 **旁路表**，避免污染对话树 leaf。

- [ ] **Step 2: SessionSync / Recall 改为读旁路状态；失败 best-effort，不阻断主路径**

- [ ] **Step 3: 恢复并改写 `tests/integrations/openviking/*`**

- [ ] **Step 4: Commit**

```bash
git commit -m "feat(p9): OpenViking 旁路状态与主 Session 解耦"
```

---

## 跨 Task 验收清单（对应 harness §19）

| 阶段 | 命令/检查 | 通过标准 |
|------|-----------|----------|
| P1 | `uv run pytest tests/persistence tests/conversations -v` | append/恢复/切 leaf/事务 |
| P2 | 序列化测试 + preview | content blocks 可还原；无 batch 落盘 |
| P3 | `tests/context/test_*.py` | 同输入唯一 ModelContext；不拆 tool 组 |
| P4 | `tests/providers -v` | 两 Provider 吃 ModelContext |
| P5 | `tests/runs/test_react_checkpoint.py` | 工具前 intent 已落盘 |
| P6 | `tests/hooks -v` | 无 Hook 行为不变；合并稳定 |
| P7 | cli/context 测试 | 不触发 Hook；与最近请求一致 |
| P8 | compaction 测试 | 多压、切点、overflow |
| 总回归 | `uv run pytest -v` | 全绿 |

---

## 执行纪律

1. **TDD：** 每个 Task 先测试后实现。  
2. **小步提交：** 每 Task 至少 1 个 commit；中文 commit message。  
3. **不做旧库迁移。**  
4. **禁止** 第二套 ModelContext 拼装路径（ReAct 与 `/context` 必须共用 Assembler）。  
5. **禁止** Hook 直接改 Session 内部字段或任意重排历史；禁止 `Hook(ModelContext)->ModelContext`。  
6. **禁止** 再引入与合同同义的新类型名（如第二套 Feedback/Outcome/Request）。  
7. **禁止** 把 `TurnState`/`StepState` 做成第二个 Session。  
8. 注释与错误信息用中文；类型名/路径保持英文。  
9. 实现前重读：  
   - `docs/upgrade/2026-07-12-query-context-harness.md`（尤其 §1 已确定/已决议/红线）  
   - 本计划对应 Task 的 Files 列表与默认决议表  

---

## 建议执行方式

本计划跨 12 个 Task、横切 conversations/context/runs/providers/persistence。推荐：

1. **Subagent-Driven Development**（每 Task 新子代理 + 双阶段审查）  
2. 或 **Inline executing-plans**（本会话分批，每 2–3 Task 停顿验收）

P0–P5 为最小可演示主路径；P6–P7 为可观测扩展；P8–P9 可按优先级后移但**不要在 P5 前插入**。Task 5–8 中间态用 shim 缩短双形状窗口，避免主分支长期不可用。
