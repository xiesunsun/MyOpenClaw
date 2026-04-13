# Session Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add local persistent session storage and resume/list CLI support while keeping `Session` as the single source of truth and preserving compatibility with a future OpenViking sync layer.

**Architecture:** Session persistence is split into four responsibilities. `conversations/` defines the session domain objects, storage mapping helpers, the repository interface, and the session application service. `persistence/` provides the SQLite implementation of local storage, `integrations/openviking/` provides a sync interface with a noop implementation for phase one, and `cli/` consumes the session service without touching SQL or OpenViking details. Runtime prompt construction remains based on in-memory `Session.messages`; only session storage and recovery are added here.

**Tech Stack:** Python 3.12, sqlite3, dataclasses, Typer, Rich, unittest, uv

---

## File Structure

- Modify: `src/myopenclaw/conversations/session.py`
  Expand the `Session` aggregate with persistence metadata while keeping message append behavior intact.

- Create: `src/myopenclaw/conversations/session_preview.py`
  Define the lightweight `SessionPreview` DTO for `openclaw sessions` and `/session`.

- Create: `src/myopenclaw/conversations/session_storage_mapper.py`
  Centralize conversion between in-memory session/message objects and SQLite-friendly records/JSON payloads.

- Create: `src/myopenclaw/conversations/repository.py`
  Define the `SessionRepository` interface for local session persistence access.

- Create: `src/myopenclaw/conversations/service.py`
  Define `SessionService`, which owns the session persistence workflow used by CLI code.

- Create: `src/myopenclaw/persistence/sqlite_session_repository.py`
  Implement `SessionRepository` using SQLite with `sessions` and `session_messages` tables.

- Create: `src/myopenclaw/integrations/openviking/session_sync.py`
  Define the `SessionSync` interface and the default `NoopSessionSync`.

- Modify: `src/myopenclaw/app/assembly.py`
  Add object construction helpers for the repository, sync implementation, and `SessionService`.

- Modify: `src/myopenclaw/cli/main.py`
  Add `openclaw sessions` and `openclaw --session-id <id>` entry points.

- Modify: `src/myopenclaw/cli/chat.py`
  Use `SessionService` to create/resume/flush/close sessions and make `/session` render `SessionPreview` data.

- Create: `tests/conversations/test_session_preview.py`
  Verify `SessionPreview` behavior and last-message generation rules.

- Create: `tests/conversations/test_session_storage_mapper.py`
  Verify round-trip mapping for session metadata and session messages.

- Create: `tests/conversations/test_session_service.py`
  Verify start/resume/flush/close behavior with fake repository and fake sync implementations.

- Create: `tests/persistence/test_sqlite_session_repository.py`
  Verify SQLite repository behavior, table schema expectations, append semantics, and list ordering.

- Modify: `tests/cli/test_chat_loop.py`
  Verify chat loop persistence hooks and `/session` output behavior.

- Modify: `tests/app/test_assembly.py`
  Verify `AppAssembly` wires `SessionService` and its dependencies.

- Create: `tests/cli/test_main_sessions.py`
  Verify `openclaw sessions` and `openclaw --session-id` CLI wiring.

## Session Model

### `Session`

Keep:
- `session_id: str`
- `agent_id: str`
- `messages: list[SessionMessage]`

Add:
- `created_at: datetime`
- `updated_at: datetime`
- `status: str`
- `remote_session_id: str | None`
- `last_synced_message_index: int | None`
- `last_committed_at: datetime | None`

Status values:
- `active`
- `closed`

### `SessionPreview`

Fields:
- `session_id: str`
- `agent_id: str`
- `created_at: datetime`
- `updated_at: datetime`
- `status: str`
- `message_count: int`
- `last_message: str`

`last_message` rules:
- Prefer the last message's `content` when non-empty.
- If the last message has empty `content` and contains `tool_call_batch`, render `"[tools] <tool names>"`.
- Normalize whitespace.
- Truncate to 50 characters and append `...` only when truncation occurs.

## SQLite Storage Schema

### `sessions`

```sql
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    status TEXT NOT NULL,
    remote_session_id TEXT,
    last_synced_message_index INTEGER,
    last_committed_at TEXT
);
```

### `session_messages`

```sql
CREATE TABLE IF NOT EXISTS session_messages (
    session_id TEXT NOT NULL,
    message_index INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_id, message_index)
);
```

Recommended indexes:

```sql
CREATE INDEX IF NOT EXISTS idx_sessions_updated_at ON sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_session_messages_session_id ON session_messages(session_id);
```

## Repository Contract

`SessionRepository` should expose:

```python
class SessionRepository(Protocol):
    def create(self, session: Session) -> None: ...
    def load(self, session_id: str) -> Session | None: ...
    def list(self, *, limit: int = 20) -> list[SessionPreview]: ...
    def append_messages(
        self,
        *,
        session_id: str,
        start_index: int,
        messages: list[SessionMessage],
        updated_at: datetime,
    ) -> None: ...
    def update_metadata(self, session: Session) -> None: ...
    def mark_closed(self, *, session_id: str, updated_at: datetime) -> None: ...
```

Semantics:
- `create()` inserts one `sessions` row and zero message rows.
- `load()` returns the full `Session` aggregate with all messages in message index order.
- `list()` returns `SessionPreview` rows ordered by `updated_at DESC`.
- `append_messages()` writes only newly created messages from one turn.
- `update_metadata()` updates `updated_at`, `status`, and the remote sync fields.
- `mark_closed()` only changes lifecycle state and timestamp.

## Session Service Contract

`SessionService` should expose:

```python
class SessionService:
    def start(self, *, agent_id: str) -> Session: ...
    def resume(self, *, session_id: str) -> Session: ...
    def list_sessions(self, *, limit: int = 20) -> list[SessionPreview]: ...
    def build_preview(self, *, session: Session) -> SessionPreview: ...
    def flush_new_messages(self, *, session: Session, start_index: int) -> None: ...
    def close(self, *, session: Session) -> None: ...
```

Semantics:
- `start()` creates an in-memory `Session`, sets timestamps/status, persists it, and returns it.
- `resume()` loads the full persisted session or raises a session-not-found error.
- `list_sessions()` delegates to the repository.
- `build_preview()` converts the current in-memory session to `SessionPreview` for `/session`.
- `flush_new_messages()` updates `updated_at`, appends new messages through the repository, persists metadata, and then calls session sync.
- `close()` marks the session `closed`, persists metadata, and then asks session sync to commit.

## Session Sync Contract

`SessionSync` should expose:

```python
class SessionSync(Protocol):
    def sync_new_messages(self, *, session: Session, start_index: int) -> None: ...
    def commit(self, *, session: Session) -> None: ...
```

Phase one implementation:
- `NoopSessionSync`
  - `sync_new_messages()` does nothing
  - `commit()` does nothing

This preserves the extension point for OpenViking without coupling phase one to external availability.

## CLI Behavior

### `openclaw sessions`

Behavior:
- Loads `SessionService` from `AppAssembly`
- Prints session previews ordered by `updated_at DESC`
- Output columns:
  - session id
  - agent id
  - status
  - message count
  - updated at
  - last message

### `openclaw --session-id <id>`

Behavior:
- Loads the persisted session first
- Reads `session.agent_id`
- Resolves the current runtime agent from `config.yaml` using that `agent_id`
- Starts chat with that session
- If the agent no longer exists in config, fail with a clear message

### `/session`

Behavior:
- Uses `SessionService.build_preview(session=...)`
- Renders the same fields as `openclaw sessions` for the current in-memory session

## Runtime Data Flow

### Start New Session
1. CLI resolves agent from config.
2. `AppAssembly.build_session_service()` returns `SessionService`.
3. `SessionService.start(agent_id=...)` creates and persists a new session.
4. `ChatLoop` runs with that session.

### Resume Persisted Session
1. CLI receives `--session-id`.
2. `SessionService.resume(session_id=...)` loads the full session.
3. CLI resolves agent from config using `session.agent_id`.
4. `ChatLoop` runs with the persisted session.

### Flush Turn Output
1. `ChatLoop` records `start_index = len(session.messages)` before the turn.
2. `AgentCoordinator` and `ReActStrategy` append user/assistant/tool messages to the in-memory session.
3. After the turn, `ChatLoop` calls `SessionService.flush_new_messages(session=session, start_index=start_index)`.
4. Service appends only the new messages to the repository, updates metadata, then calls `SessionSync.sync_new_messages(...)`.

### Close Session
1. `ChatLoop` calls `SessionService.close(session=session)` on `/exit` or EOF/interrupt.
2. Service marks the session closed through the repository.
3. Service calls `SessionSync.commit(...)`.

## Task Plan

### Task 1: Extend Session Metadata

**Files:**
- Modify: `src/myopenclaw/conversations/session.py`
- Test: `tests/conversations/test_session.py`

- [ ] **Step 1: Write failing tests for persistence metadata fields**

```python
def test_session_create_populates_persistence_metadata(self) -> None:
    session = Session.create(agent_id="Pickle", session_id="session-1")
    self.assertEqual("active", session.status)
    self.assertIsNotNone(session.created_at)
    self.assertIsNotNone(session.updated_at)
    self.assertIsNone(session.remote_session_id)
    self.assertIsNone(session.last_synced_message_index)
    self.assertIsNone(session.last_committed_at)
```

- [ ] **Step 2: Run the session tests to verify failure**

Run: `PYTHONPATH=src uv run python -m unittest tests.conversations.test_session -v`
Expected: FAIL because the new fields do not exist.

- [ ] **Step 3: Extend `Session` with metadata fields and sensible defaults**

```python
@dataclass
class Session:
    session_id: str
    agent_id: str
    messages: list[SessionMessage] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "active"
    remote_session_id: str | None = None
    last_synced_message_index: int | None = None
    last_committed_at: datetime | None = None
```

- [ ] **Step 4: Add a small helper to bump `updated_at`**

```python
def touch(self, *, at: datetime | None = None) -> None:
    self.updated_at = at or datetime.now(timezone.utc)
```

- [ ] **Step 5: Run the session tests**

Run: `PYTHONPATH=src uv run python -m unittest tests.conversations.test_session -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/myopenclaw/conversations/session.py tests/conversations/test_session.py
git commit -m "feat: add session persistence metadata"
```

### Task 2: Add Session Preview

**Files:**
- Create: `src/myopenclaw/conversations/session_preview.py`
- Create: `tests/conversations/test_session_preview.py`

- [ ] **Step 1: Write failing tests for preview construction rules**

```python
def test_last_message_uses_content_and_truncates(self) -> None:
    preview = SessionPreview(
        session_id="session-1",
        agent_id="Pickle",
        created_at=created_at,
        updated_at=updated_at,
        status="active",
        message_count=3,
        last_message="x" * 60,
    )
    self.assertTrue(preview.last_message.endswith("..."))
```

```python
def test_last_message_can_hold_tool_preview(self) -> None:
    self.assertEqual("[tools] read_file, grep_search", preview.last_message)
```

- [ ] **Step 2: Run the preview tests to verify failure**

Run: `PYTHONPATH=src uv run python -m unittest tests.conversations.test_session_preview -v`
Expected: FAIL because the file and class do not exist.

- [ ] **Step 3: Create `SessionPreview`**

```python
@dataclass(frozen=True)
class SessionPreview:
    session_id: str
    agent_id: str
    created_at: datetime
    updated_at: datetime
    status: str
    message_count: int
    last_message: str
```

- [ ] **Step 4: Run the preview tests**

Run: `PYTHONPATH=src uv run python -m unittest tests.conversations.test_session_preview -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/myopenclaw/conversations/session_preview.py tests/conversations/test_session_preview.py
git commit -m "feat: add session preview model"
```

### Task 3: Add Session Storage Mapping

**Files:**
- Create: `src/myopenclaw/conversations/session_storage_mapper.py`
- Create: `tests/conversations/test_session_storage_mapper.py`

- [ ] **Step 1: Write failing round-trip tests for message mapping**

```python
def test_session_message_round_trips_through_storage_record(self) -> None:
    record = session_message_to_record(message=message)
    restored = session_message_from_record(record)
    self.assertEqual(message.role, restored.role)
    self.assertEqual(message.tool_call_batch.calls[0].name, restored.tool_call_batch.calls[0].name)
```

```python
def test_session_preview_last_message_prefers_tool_names_when_content_is_empty(self) -> None:
    preview = build_session_preview(session=session)
    self.assertEqual("[tools] read_file", preview.last_message)
```

- [ ] **Step 2: Run the mapper tests to verify failure**

Run: `PYTHONPATH=src uv run python -m unittest tests.conversations.test_session_storage_mapper -v`
Expected: FAIL because the mapper file does not exist.

- [ ] **Step 3: Implement storage mapping helpers**

Implement helpers for:
- session metadata row creation
- session message row creation
- session message JSON serialization
- full session reconstruction from one metadata row plus ordered message rows
- preview building

- [ ] **Step 4: Run the mapper tests**

Run: `PYTHONPATH=src uv run python -m unittest tests.conversations.test_session_storage_mapper -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/myopenclaw/conversations/session_storage_mapper.py tests/conversations/test_session_storage_mapper.py
git commit -m "feat: add session storage mapping helpers"
```

### Task 4: Define Repository Interface and SQLite Implementation

**Files:**
- Create: `src/myopenclaw/conversations/repository.py`
- Create: `src/myopenclaw/persistence/sqlite_session_repository.py`
- Create: `tests/persistence/test_sqlite_session_repository.py`

- [ ] **Step 1: Write failing repository tests**

```python
def test_create_and_load_round_trip(self) -> None:
    repo.create(session)
    loaded = repo.load("session-1")
    self.assertEqual("Pickle", loaded.agent_id)
    self.assertEqual(["hello"], [message.content for message in loaded.messages])
```

```python
def test_list_returns_session_previews_in_updated_order(self) -> None:
    previews = repo.list(limit=20)
    self.assertEqual(["session-2", "session-1"], [preview.session_id for preview in previews])
```

```python
def test_append_messages_only_writes_new_range(self) -> None:
    repo.append_messages(session_id="session-1", start_index=1, messages=session.messages[1:], updated_at=updated_at)
    loaded = repo.load("session-1")
    self.assertEqual(2, len(loaded.messages))
```

- [ ] **Step 2: Run the repository tests to verify failure**

Run: `PYTHONPATH=src uv run python -m unittest tests.persistence.test_sqlite_session_repository -v`
Expected: FAIL because the interface and SQLite implementation do not exist.

- [ ] **Step 3: Define `SessionRepository`**

```python
class SessionRepository(Protocol):
    ...
```

- [ ] **Step 4: Implement `SQLiteSessionRepository`**

Implementation checklist:
- open connection
- create tables lazily
- use transactions for create/append
- delegate mapping to `session_storage_mapper`

- [ ] **Step 5: Run the repository tests**

Run: `PYTHONPATH=src uv run python -m unittest tests.persistence.test_sqlite_session_repository -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/myopenclaw/conversations/repository.py src/myopenclaw/persistence/sqlite_session_repository.py tests/persistence/test_sqlite_session_repository.py
git commit -m "feat: add sqlite session repository"
```

### Task 5: Add Session Sync Interface

**Files:**
- Create: `src/myopenclaw/integrations/openviking/session_sync.py`
- Create: `tests/conversations/test_session_service.py`

- [ ] **Step 1: Write failing tests that use a fake sync implementation**

```python
def test_flush_new_messages_calls_session_sync(self) -> None:
    service.flush_new_messages(session=session, start_index=0)
    self.assertEqual([0], fake_sync.synced_start_indexes)
```

```python
def test_close_calls_commit(self) -> None:
    service.close(session=session)
    self.assertTrue(fake_sync.committed)
```

- [ ] **Step 2: Run the service tests to verify failure**

Run: `PYTHONPATH=src uv run python -m unittest tests.conversations.test_session_service -v`
Expected: FAIL because the sync interface and service do not exist.

- [ ] **Step 3: Create `SessionSync` and `NoopSessionSync`**

```python
class SessionSync(Protocol):
    def sync_new_messages(self, *, session: Session, start_index: int) -> None: ...
    def commit(self, *, session: Session) -> None: ...
```

```python
class NoopSessionSync:
    def sync_new_messages(self, *, session: Session, start_index: int) -> None:
        return None

    def commit(self, *, session: Session) -> None:
        return None
```

- [ ] **Step 4: Run the service tests again**

Run: `PYTHONPATH=src uv run python -m unittest tests.conversations.test_session_service -v`
Expected: still FAIL because `SessionService` is not yet implemented.

- [ ] **Step 5: Commit**

```bash
git add src/myopenclaw/integrations/openviking/session_sync.py tests/conversations/test_session_service.py
git commit -m "feat: add session sync interface"
```

### Task 6: Implement Session Service

**Files:**
- Create: `src/myopenclaw/conversations/service.py`
- Modify: `tests/conversations/test_session_service.py`

- [ ] **Step 1: Add the remaining failing tests for start/resume/list/preview**

```python
def test_start_creates_and_persists_active_session(self) -> None:
    session = service.start(agent_id="Pickle")
    self.assertEqual("active", session.status)
    self.assertEqual(session, fake_repo.loaded["session-id"])
```

```python
def test_resume_loads_existing_session(self) -> None:
    loaded = service.resume(session_id="session-1")
    self.assertEqual("session-1", loaded.session_id)
```

```python
def test_build_preview_uses_last_message_rules(self) -> None:
    preview = service.build_preview(session=session)
    self.assertEqual("hello", preview.last_message)
```

- [ ] **Step 2: Run the service tests to verify failure**

Run: `PYTHONPATH=src uv run python -m unittest tests.conversations.test_session_service -v`
Expected: FAIL because `SessionService` does not exist.

- [ ] **Step 3: Implement `SessionService`**

Implementation checklist:
- inject `SessionRepository`
- inject `SessionSync`
- generate new session ids
- set timestamps
- update `updated_at` before persistence flush
- call repo first, sync second

- [ ] **Step 4: Run the service tests**

Run: `PYTHONPATH=src uv run python -m unittest tests.conversations.test_session_service -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/myopenclaw/conversations/service.py tests/conversations/test_session_service.py
git commit -m "feat: add session service"
```

### Task 7: Wire Assembly

**Files:**
- Modify: `src/myopenclaw/app/assembly.py`
- Modify: `tests/app/test_assembly.py`

- [ ] **Step 1: Write failing assembly tests**

```python
def test_build_session_service_returns_session_service_with_sqlite_repo(self) -> None:
    assembly = AppAssembly.from_config_path(config_path)
    service = assembly.build_session_service()
    self.assertIsNotNone(service)
```

- [ ] **Step 2: Run the assembly tests to verify failure**

Run: `PYTHONPATH=src uv run python -m unittest tests.app.test_assembly -v`
Expected: FAIL because `build_session_service()` does not exist.

- [ ] **Step 3: Implement `build_session_service()` in `AppAssembly`**

Implementation checklist:
- choose session database path under app/config root
- construct `SQLiteSessionRepository`
- construct `NoopSessionSync`
- construct `SessionService`

- [ ] **Step 4: Run the assembly tests**

Run: `PYTHONPATH=src uv run python -m unittest tests.app.test_assembly -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/myopenclaw/app/assembly.py tests/app/test_assembly.py
git commit -m "feat: wire session service in app assembly"
```

### Task 8: Add CLI Entry Points

**Files:**
- Modify: `src/myopenclaw/cli/main.py`
- Create: `tests/cli/test_main_sessions.py`

- [ ] **Step 1: Write failing CLI tests**

```python
def test_sessions_command_lists_previews(self) -> None:
    result = runner.invoke(app, ["sessions", "--config", str(config_path)])
    assert result.exit_code == 0
    assert "session-1" in result.stdout
```

```python
def test_session_id_option_resumes_existing_session(self) -> None:
    result = runner.invoke(app, ["--config", str(config_path), "--session-id", "session-1"])
    assert result.exit_code == 0
```

- [ ] **Step 2: Run the CLI tests to verify failure**

Run: `PYTHONPATH=src uv run python -m unittest tests.cli.test_main_sessions -v`
Expected: FAIL because the new command and option do not exist.

- [ ] **Step 3: Implement CLI entry points**

Implementation checklist:
- add `session_id: str | None = typer.Option(None, "--session-id")`
- add `@app.command("sessions")`
- print preview rows with simple rich/text output

- [ ] **Step 4: Run the CLI tests**

Run: `PYTHONPATH=src uv run python -m unittest tests.cli.test_main_sessions -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/myopenclaw/cli/main.py tests/cli/test_main_sessions.py
git commit -m "feat: add session list and resume cli entry points"
```

### Task 9: Integrate Session Service into Chat Loop

**Files:**
- Modify: `src/myopenclaw/cli/chat.py`
- Modify: `tests/cli/test_chat_loop.py`

- [ ] **Step 1: Write failing chat loop tests**

```python
async def test_run_flushes_new_messages_after_turn(self) -> None:
    ...
    self.assertEqual([(0, 2)], fake_session_service.flush_calls)
```

```python
async def test_run_closes_session_on_exit(self) -> None:
    ...
    self.assertTrue(fake_session_service.closed)
```

```python
async def test_session_command_renders_preview(self) -> None:
    ...
    self.assertIn("session-1", rendered)
    self.assertIn("runtime reply", rendered)
```

- [ ] **Step 2: Run the chat loop tests to verify failure**

Run: `PYTHONPATH=src uv run python -m unittest tests.cli.test_chat_loop -v`
Expected: FAIL because `ChatLoop` does not use `SessionService`.

- [ ] **Step 3: Inject `SessionService` into `ChatLoop`**

Implementation checklist:
- constructor accepts `session_service: SessionService | None = None`
- `from_config_path()` builds/uses `SessionService`
- after each successful turn:
  - capture `start_index` before request
  - flush new messages after request
- on exit:
  - close session
- `/session` calls `build_preview(session=...)`

- [ ] **Step 4: Run the chat loop tests**

Run: `PYTHONPATH=src uv run python -m unittest tests.cli.test_chat_loop -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/myopenclaw/cli/chat.py tests/cli/test_chat_loop.py
git commit -m "feat: persist chat sessions through session service"
```

### Task 10: End-to-End Validation

**Files:**
- No new files

- [ ] **Step 1: Run the targeted test suite**

Run:

```bash
PYTHONPATH=src uv run python -m unittest \
  tests.conversations.test_session \
  tests.conversations.test_session_preview \
  tests.conversations.test_session_storage_mapper \
  tests.conversations.test_session_service \
  tests.persistence.test_sqlite_session_repository \
  tests.cli.test_main_sessions \
  tests.cli.test_chat_loop \
  tests.app.test_assembly
```

Expected: PASS

- [ ] **Step 2: Manual smoke test**

Run:

```bash
PYTHONPATH=src uv run python -m myopenclaw.cli.main --config config.yaml
```

Manual steps:
- start a new chat
- send one user message
- exit
- run `openclaw sessions`
- confirm the session appears
- run `openclaw --session-id <id>`
- confirm the chat resumes with the prior history

- [ ] **Step 3: Final commit**

```bash
git add src tests
git commit -m "feat: add local session persistence and resume flow"
```

## Notes

- Do not add session persistence logic to the context layer. Context remains a pure projection from `Session.messages`.
- Do not let CLI code write SQL directly.
- Do not let `SessionService` know about SQLite details.
- Do not treat OpenViking as the source of truth for phase one.
- Keep `AppAssembly` as the composition root, but only add object construction helpers there, not business logic.
