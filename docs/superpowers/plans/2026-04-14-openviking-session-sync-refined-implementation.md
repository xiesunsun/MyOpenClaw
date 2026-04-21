# OpenViking Session Sync Refined Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add refined OpenViking session sync for `myopenclaw`: keep local SQLite as the runtime source of truth, sync pending messages to OpenViking after each turn, commit pending remote session state on exit or policy thresholds, and preserve single-user production assumptions while reserving hooks for future multi-user support.

**Architecture:** The chat loop and runtime stay unchanged at the top level: each turn still appends to the in-memory `Session`, then `SessionService` persists the local turn delta to SQLite. OpenViking integration is isolated behind `SessionSync`; the real implementation computes pending remote deltas from sync watermarks stored on `Session`, maps local `SessionMessage` objects into OpenViking-compatible payloads, and uses a dedicated client adapter plus commit policy to drive remote writes and commits. Current production scope is single-user, but `Session` metadata and config objects record OpenViking account/user/agent binding so later multi-user work can be added without redesigning persistence or sync watermarks.

**Tech Stack:** Python 3.12, SQLite, Pydantic, Rich/Typer CLI, OpenViking Python SDK (`openviking`), unittest

---

## 1. Scope And Non-Goals

### In Scope

- Keep local SQLite session persistence as the authority for runtime recovery.
- Sync pending local messages to OpenViking after each completed turn.
- Commit the remote OpenViking session on close, plus optional time/turn threshold commits.
- Reuse the local `session_id` as the remote OpenViking `session_id`.
- Persist remote sync metadata locally so failed syncs can be retried on the next turn.
- Support current production deployment assumptions:
  - single OpenViking user
  - user key auth
  - one or more local agents mapped to explicit remote `agent_id`s
- Reserve metadata/config hooks for future multi-user support.

### Out Of Scope

- Multi-user runtime switching.
- Using `ROOT_KEY` in the normal path.
- OpenViking `used(contexts=..., skill=...)` tracking.
- Injecting OpenViking session history directly into prompts.
- Implementing long-term recall or `search(session_id=...)` in this phase.
- Changing the local prompt assembly model.

## 2. Current State Summary

### Local Session Lifecycle

- `ChatLoop.run()` captures `start_index = len(session.messages)` before each turn and calls `SessionService.flush_new_messages(session, start_index)` after a successful turn.
- `SessionService.flush_new_messages()` appends the local turn delta to SQLite, updates metadata, then calls `SessionSync`.
- `SessionService.close()` marks the session closed and calls `SessionSync.commit(...)`.

### Existing Remote-Ready Metadata

`Session` already contains:

- `remote_session_id`
- `last_synced_message_index`
- `last_committed_at`

These already persist through:

- `session_storage_mapper.py`
- `SQLiteSessionRepository`

### Structural Gaps

- There is no commit watermark for remote state. We cannot tell which synced messages have already been committed remotely.
- `SessionSync.sync_new_messages(session, start_index)` mixes local append delta with remote unsynced delta.
- There is no OpenViking-specific config object or client adapter.
- There is no message mapper from `SessionMessage` to OpenViking `content/parts`.
- There is no commit policy abstraction.
- The current repository schema lacks:
  - `last_committed_message_index`
  - `openviking_account_id`
  - `openviking_user_id`
  - `openviking_agent_id`

## 3. Production Assumptions For This Phase

This plan assumes the current deployed OpenViking server configuration:

- `OPENVIKING_BASE_URL=https://openviking.sunxie.me`
- one production account/user:
  - `account_id=myopenclaw`
  - `user_id=ssunxie`
- user-key auth for normal traffic
- explicit `agent_id` from the client
- server-side `agent_scope_mode=user+agent`

Practical meaning:

- the current implementation should treat OpenViking config as single-user
- one user can have multiple remote agents
- different users would not share the same remote agent space even if `agent_id` matches
- we must record `account_id/user_id/agent_id` on the local session for future consistency checks

## 4. Target Layering

### `cli/`

Responsibility:

- collect user input
- call `SessionService`
- render output

Files:

- Modify: `src/myopenclaw/cli/chat.py`

### `conversations/`

Responsibility:

- session domain model
- sync watermarks
- application service orchestration
- repository and sync protocol definitions

Files:

- Modify: `src/myopenclaw/conversations/session.py`
- Modify: `src/myopenclaw/conversations/service.py`
- Modify: `src/myopenclaw/conversations/session_storage_mapper.py`
- Modify: `src/myopenclaw/conversations/repository.py`
- Modify: `src/myopenclaw/integrations/openviking/session_sync.py`

### `persistence/`

Responsibility:

- SQLite schema
- local metadata and message persistence

Files:

- Modify: `src/myopenclaw/persistence/sqlite_session_repository.py`

### `integrations/openviking/`

Responsibility:

- config resolution
- OpenViking SDK wrapping
- local-to-remote message translation
- commit rules
- concrete session sync implementation

Files:

- Create: `src/myopenclaw/integrations/openviking/config.py`
- Create: `src/myopenclaw/integrations/openviking/session_client.py`
- Create: `src/myopenclaw/integrations/openviking/session_message_mapper.py`
- Create: `src/myopenclaw/integrations/openviking/commit_policy.py`
- Modify: `src/myopenclaw/integrations/openviking/session_sync.py`

### `app/` and `config/`

Responsibility:

- configuration model
- composition root

Files:

- Modify: `src/myopenclaw/config/app_config.py`
- Modify: `src/myopenclaw/app/assembly.py`

## 5. Final File Structure

### Modify Existing

- `src/myopenclaw/cli/chat.py`
- `src/myopenclaw/conversations/session.py`
- `src/myopenclaw/conversations/service.py`
- `src/myopenclaw/conversations/session_storage_mapper.py`
- `src/myopenclaw/conversations/repository.py`
- `src/myopenclaw/persistence/sqlite_session_repository.py`
- `src/myopenclaw/integrations/openviking/session_sync.py`
- `src/myopenclaw/config/app_config.py`
- `src/myopenclaw/app/assembly.py`
- `tests/conversations/test_session.py`
- `tests/conversations/test_session_service.py`
- `tests/conversations/test_session_storage_mapper.py`
- `tests/persistence/test_sqlite_session_repository.py`
- `tests/app/test_assembly.py`
- `tests/cli/test_chat_loop.py`

### Create New

- `src/myopenclaw/integrations/openviking/config.py`
- `src/myopenclaw/integrations/openviking/session_client.py`
- `src/myopenclaw/integrations/openviking/session_message_mapper.py`
- `src/myopenclaw/integrations/openviking/commit_policy.py`
- `tests/integrations/openviking/test_session_message_mapper.py`
- `tests/integrations/openviking/test_commit_policy.py`
- `tests/integrations/openviking/test_openviking_session_sync.py`

## 6. Domain Model Changes

### `Session`

Keep existing fields:

- `remote_session_id: str | None`
- `last_synced_message_index: int | None`
- `last_committed_at: datetime | None`

Add fields:

- `last_committed_message_index: int | None = None`
- `openviking_account_id: str | None = None`
- `openviking_user_id: str | None = None`
- `openviking_agent_id: str | None = None`

Reasoning:

- `remote_session_id` stays as the remote binding, even though in practice it will match the local `session_id`.
- `last_synced_message_index` remains the sync watermark.
- `last_committed_at` remains useful for policy checks and observability.
- `last_committed_message_index` is required to distinguish:
  - synced to OpenViking
  - committed by OpenViking
- `openviking_account_id/user_id/agent_id` are recorded even in single-user mode so future multi-user support can validate session affinity.

### `Session` methods to add

- `bind_openviking(account_id: str, user_id: str, agent_id: str) -> None`
- `pending_sync_start_index() -> int`
- `pending_sync_messages() -> list[SessionMessage]`
- `has_pending_remote_commit() -> bool`
- `mark_messages_synced(*, remote_session_id: str, last_message_index: int) -> None`
- `mark_messages_committed(*, last_message_index: int, committed_at: datetime) -> None`

Watermark semantics:

- `last_synced_message_index` is the highest local message index that exists remotely.
- `last_committed_message_index` is the highest local message index whose remote state has been committed.
- Invariant: `last_committed_message_index <= last_synced_message_index` whenever both are non-`None`.

## 7. Key Concepts And Exact Meanings

### `start_index`

`start_index` remains a local-persistence-only concept.

Definition:

- `start_index = len(session.messages)` immediately before the current turn starts.

Use:

- determine which messages were added during this turn for SQLite append

Example:

- before turn: `len(messages) == 5`
- current turn adds:
  - one user message
  - one assistant message with a tool batch
  - one final assistant message
- after turn: `len(messages) == 8`
- local turn delta is `messages[5:]`

Important:

- one user turn does not always equal two session messages
- a tool batch is stored as one assistant message containing `tool_call_batch`
- therefore message count is not the same as turn count

### `pending`

Within `SessionSync`, `pending` means:

- already persisted in local session state
- not yet reflected in the relevant remote watermark

There are two pending concepts:

- pending sync:
  messages after `last_synced_message_index`
- pending commit:
  synced remote messages beyond `last_committed_message_index`

## 8. OpenViking Message Mapping

### Mapping Rules

#### User message

Local:

- `role=user`
- `content=<text>`

Remote:

- `role="user"`
- `parts=[{"type": "text", "text": <content>}]`

#### Assistant message without tools

Local:

- `role=assistant`
- `content=<text>`
- `tool_call_batch=None`

Remote:

- `role="assistant"`
- `parts=[{"type": "text", "text": <content>}]`

#### Assistant message with tool batch

Local:

- `role=assistant`
- `tool_call_batch=<batch>`
- optional `content`

Remote:

- `role="assistant"`
- `parts` contains:
  - leading `TextPart` if `content` is non-empty
  - one `ToolPart` per tool call/result pair

Tool part mapping:

- `tool_id <- ToolCall.id`
- `tool_name <- ToolCall.name`
- `tool_input <- ToolCall.arguments`
- `tool_output <- truncated ToolCallResult.content`
- `tool_status <- "completed"` if success else `"error"`

### V1 exclusions

Do not include in this phase:

- `ContextPart`
- `used(contexts=..., skill=...)`
- provider metadata
- token accounting inside tool parts unless already directly available

### Why mapping lives in its own file

- it isolates OpenViking payload logic from `SessionService`
- it keeps SDK-specific structure out of `conversations/`
- it can be tested independently from persistence and network behavior

## 9. Interfaces

### `SessionSync`

```python
class SessionSync(Protocol):
    def sync_pending_messages(self, *, session: Session) -> None: ...
    def commit_pending_messages(
        self,
        *,
        session: Session,
        force: bool = False,
    ) -> None: ...
```

Rationale:

- `start_index` is not passed here because remote sync must derive pending work from sync watermarks, not from the current turn delta

### `OpenVikingSessionClient`

```python
class OpenVikingSessionClient(Protocol):
    def ensure_session(self, *, session_id: str) -> str: ...

    def append_message(
        self,
        *,
        session_id: str,
        role: str,
        content: str | None = None,
        parts: list[dict] | None = None,
    ) -> None: ...

    def commit_session(self, *, session_id: str) -> None: ...
```

`ensure_session()` meaning:

- return a writable remote session id
- if remote session already exists, reuse it
- if it does not exist, create it using the provided `session_id`

For this phase:

- local `session.session_id == remote_session_id`

### `CommitPolicy`

```python
class CommitPolicy(Protocol):
    def should_commit(self, *, session: Session, now: datetime) -> bool: ...
```

Default rule for this phase:

- never commit if there is no pending remote commit
- commit if `force=True`
- otherwise commit when either:
  - enough time has passed since `last_committed_at`
  - enough assistant turns have accumulated since `last_committed_message_index`

## 10. Coupling Rules

Allowed coupling:

- `ChatLoop -> SessionService`
- `SessionService -> SessionRepository`
- `SessionService -> SessionSync`
- `OpenVikingSessionSync -> OpenVikingSessionClient`
- `OpenVikingSessionSync -> SessionMessageMapper`
- `OpenVikingSessionSync -> CommitPolicy`
- `SQLiteSessionRepository -> session_storage_mapper`
- `AppAssembly -> AppConfig/OpenVikingConfig/concrete implementations`

Forbidden coupling:

- `ChatLoop -> OpenViking SDK`
- `SQLiteSessionRepository -> OpenViking`
- `SessionMessageMapper -> SQLite`
- `CommitPolicy -> CLI`
- `SessionSync -> ChatLoop start_index`

## 11. End-To-End Data Flow

### A. Successful turn without tools

1. `ChatLoop.run()` stores `start_index`.
2. Coordinator appends:
   - user message
   - assistant message
3. `SessionService.flush_new_messages(session, start_index)`:
   - append local delta to SQLite
   - update `updated_at`
   - call `SessionSync.sync_pending_messages(session)`
4. `OpenVikingSessionSync`:
   - bind OpenViking identity if missing
   - compute pending sync window from `last_synced_message_index`
   - call `ensure_session(session_id=session.session_id)`
   - map and append each pending message remotely
   - update `remote_session_id` and `last_synced_message_index`
   - if policy triggers, call `commit_pending_messages(force=False)`
5. `SessionService` writes updated session metadata back to SQLite.

### B. Successful turn with tools

1. `ChatLoop.run()` stores `start_index`.
2. ReAct appends:
   - user message
   - assistant message with `tool_call_batch`
   - final assistant message
3. `SessionService` still appends `messages[start_index:]` locally.
4. `SessionSync.sync_pending_messages(session)` exports three remote messages:
   - user text
   - assistant `parts=[TextPart?, ToolPart...]`
   - assistant final text

### C. Close / exit

1. `SessionService.close(session)` marks the session closed locally.
2. It calls `sync_pending_messages(session)` to flush any remaining unsynced remote messages.
3. It calls `commit_pending_messages(session, force=True)`.
4. On success:
   - `last_committed_message_index = last_synced_message_index`
   - `last_committed_at = now`
5. It writes updated metadata back to SQLite.

### D. Remote sync failure

1. Local SQLite append already succeeded.
2. `SessionSync.sync_pending_messages(session)` raises or handles an OpenViking client failure.
3. The implementation must treat OpenViking failure as best-effort:
   - local session remains valid
   - sync watermarks do not advance
   - next turn retries from `pending_sync_start_index()`

## 12. SQLite Schema Changes

### `sessions` table additions

- `last_committed_message_index INTEGER`
- `openviking_account_id TEXT`
- `openviking_user_id TEXT`
- `openviking_agent_id TEXT`

### Required repository changes

`SQLiteSessionRepository` must update:

- `INSERT INTO sessions`
- `SELECT ... FROM sessions`
- `list()`
- `update_metadata()`
- schema initialization

### Migration note

The current `_ensure_schema()` only creates missing tables. Existing users may already have a `sessions.db`.

Therefore add lightweight column migration logic:

- inspect current table columns with `PRAGMA table_info(sessions)`
- `ALTER TABLE sessions ADD COLUMN ...` for each missing new column

Without this, existing local databases will not pick up the new metadata columns.

## 13. Config Design

### New config models

Create `src/myopenclaw/integrations/openviking/config.py` with:

```python
class OpenVikingAgentConfig(BaseModel):
    remote_agent_id: str
    enabled: bool = True


class OpenVikingConfig(BaseModel):
    enabled: bool = False
    base_url: str
    account_id: str
    user_id: str
    user_key: str
    timeout_seconds: float = 30.0
    commit_after_minutes: int = 30
    commit_after_turns: int = 8
    tool_output_max_chars: int = 4000
    agents: dict[str, OpenVikingAgentConfig] = Field(default_factory=dict)
```

### Why include account/user fields in single-user mode

- they reflect production deployment assumptions
- they are saved onto `Session`
- they reserve the later multi-user expansion point
- they make sync binding explicit in logs and debugging

### Why this still counts as single-user

- only one `base_url/account_id/user_id/user_key` block is active
- the only per-agent variation is `remote_agent_id`
- there is no per-session profile selection yet

## 14. App Assembly Design

`AppAssembly` is the composition root. It is not business logic.

Its responsibility here:

- resolve local repository implementation
- resolve OpenViking config
- decide whether session sync is noop or real
- wire the real sync implementation with all its collaborators

`build_session_service()` should:

1. build `SQLiteSessionRepository`
2. if OpenViking disabled:
   - use `NoopSessionSync`
3. if OpenViking enabled:
   - resolve the local agent id
   - look up its `remote_agent_id`
   - build:
     - `OpenVikingSessionClient`
     - `SessionMessageMapper`
     - `CommitPolicy`
     - `OpenVikingSessionSync`
4. return `SessionService(repository, session_sync, ...)`

## 15. Required Code Changes By File

### `src/myopenclaw/conversations/session.py`

- Add four new fields:
  - `last_committed_message_index`
  - `openviking_account_id`
  - `openviking_user_id`
  - `openviking_agent_id`
- Add helper methods described above.

### `src/myopenclaw/conversations/service.py`

- Keep `flush_new_messages(session, start_index)` signature.
- Change internal flow:
  - append local delta
  - update local `updated_at`
  - call `sync_pending_messages(session)`
  - write final metadata after sync/commit side effects
- Change `close(session)` flow:
  - mark local closed
  - call `sync_pending_messages(session)`
  - call `commit_pending_messages(session, force=True)`
  - persist final metadata

### `src/myopenclaw/integrations/openviking/session_sync.py`

- Keep `SessionSync` protocol name.
- Rename protocol methods to:
  - `sync_pending_messages`
  - `commit_pending_messages`
- Add `OpenVikingSessionSync`
- Keep `NoopSessionSync` with matching no-op methods

### `src/myopenclaw/integrations/openviking/session_client.py`

- Implement an SDK-backed adapter around `openviking.SyncHTTPClient`
- `ensure_session()` should:
  - try to get the remote session
  - create it if missing
  - return the remote id

### `src/myopenclaw/integrations/openviking/session_message_mapper.py`

- Export `SessionMessage` into OpenViking `content/parts`
- centralize tool output truncation

### `src/myopenclaw/integrations/openviking/commit_policy.py`

- implement the default time/turn threshold logic

### `src/myopenclaw/persistence/sqlite_session_repository.py`

- add schema columns
- add migration logic for existing DBs
- read/write new metadata fields

### `src/myopenclaw/conversations/session_storage_mapper.py`

- map the new metadata fields both ways

### `src/myopenclaw/config/app_config.py`

- add optional `openviking` section
- validate referenced agent ids if needed

### `src/myopenclaw/app/assembly.py`

- construct real OpenViking sync dependencies when enabled

## 16. Test Strategy

### Unit tests

Add or update tests for:

- `Session`
  - watermark helpers
  - OpenViking binding helpers
- `session_storage_mapper`
  - roundtrip new fields
- `SQLiteSessionRepository`
  - create/load/list/update with new metadata
  - migration path from old schema
- `SessionMessageMapper`
  - plain user message
  - plain assistant message
  - assistant message with tool batch
  - tool output truncation
- `CommitPolicy`
  - no pending commit
  - time threshold
  - turn threshold
  - forced commit
- `OpenVikingSessionSync`
  - first sync creates/ensures remote session
  - sync advances watermark
  - failed sync preserves watermark
  - commit advances commit watermark
  - force commit on close

### Integration-style tests with fakes

Use fake session client and fake repository to verify:

- `SessionService.flush_new_messages()` calls local append and then sync
- `SessionService.close()` syncs then commits then persists metadata
- `ChatLoop.run()` still uses `start_index` for local append delta only

### Manual validation

1. Start a fresh chat and complete one no-tool turn.
2. Confirm local SQLite has messages and OpenViking sync metadata.
3. Start a turn that uses a tool and confirm remote message parts include tool data.
4. Exit cleanly and confirm commit watermark is updated.
5. Reopen the same local session and continue chatting.
6. Simulate remote failure and verify next turn retries pending sync.

## 17. Execution Tasks

### Task 1: Extend Session Metadata Model

**Files:**
- Modify: `src/myopenclaw/conversations/session.py`
- Test: `tests/conversations/test_session.py`

- [ ] **Step 1: Write failing tests for new Session fields and helper methods**
- [ ] **Step 2: Run tests to verify failure**
- [ ] **Step 3: Implement the minimal Session field additions and helpers**
- [ ] **Step 4: Run tests to verify pass**
- [ ] **Step 5: Commit**

Suggested commit:

```bash
git add src/myopenclaw/conversations/session.py tests/conversations/test_session.py
git commit -m "feat: extend session metadata for openviking sync"
```

### Task 2: Persist New Metadata Fields And Migrate Schema

**Files:**
- Modify: `src/myopenclaw/conversations/session_storage_mapper.py`
- Modify: `src/myopenclaw/persistence/sqlite_session_repository.py`
- Test: `tests/conversations/test_session_storage_mapper.py`
- Test: `tests/persistence/test_sqlite_session_repository.py`

- [ ] **Step 1: Write failing mapper and repository tests for the new columns**
- [ ] **Step 2: Add schema migration coverage for old DB shape**
- [ ] **Step 3: Implement mapper updates and repository schema migration**
- [ ] **Step 4: Run repository and mapper tests**
- [ ] **Step 5: Commit**

Suggested commit:

```bash
git add \
  src/myopenclaw/conversations/session_storage_mapper.py \
  src/myopenclaw/persistence/sqlite_session_repository.py \
  tests/conversations/test_session_storage_mapper.py \
  tests/persistence/test_sqlite_session_repository.py
git commit -m "feat: persist openviking commit metadata"
```

### Task 3: Introduce OpenViking Config Models

**Files:**
- Create: `src/myopenclaw/integrations/openviking/config.py`
- Modify: `src/myopenclaw/config/app_config.py`
- Test: `tests/app/test_assembly.py`

- [ ] **Step 1: Write failing tests for parsing single-user OpenViking config**
- [ ] **Step 2: Implement OpenViking config models and app config integration**
- [ ] **Step 3: Run config-related tests**
- [ ] **Step 4: Commit**

Suggested commit:

```bash
git add \
  src/myopenclaw/integrations/openviking/config.py \
  src/myopenclaw/config/app_config.py \
  tests/app/test_assembly.py
git commit -m "feat: add openviking session sync config"
```

### Task 4: Add OpenViking Session Client Adapter

**Files:**
- Create: `src/myopenclaw/integrations/openviking/session_client.py`
- Test: `tests/integrations/openviking/test_openviking_session_sync.py`

- [ ] **Step 1: Add failing tests around ensure/create/append/commit behavior using fakes or mocks**
- [ ] **Step 2: Implement the SDK adapter**
- [ ] **Step 3: Run adapter tests**
- [ ] **Step 4: Commit**

Suggested commit:

```bash
git add \
  src/myopenclaw/integrations/openviking/session_client.py \
  tests/integrations/openviking/test_openviking_session_sync.py
git commit -m "feat: add openviking session client adapter"
```

### Task 5: Add SessionMessage Mapper And Commit Policy

**Files:**
- Create: `src/myopenclaw/integrations/openviking/session_message_mapper.py`
- Create: `src/myopenclaw/integrations/openviking/commit_policy.py`
- Test: `tests/integrations/openviking/test_session_message_mapper.py`
- Test: `tests/integrations/openviking/test_commit_policy.py`

- [ ] **Step 1: Write failing mapping tests for plain and tool-batch messages**
- [ ] **Step 2: Write failing commit policy tests**
- [ ] **Step 3: Implement mapper and policy**
- [ ] **Step 4: Run mapper and policy tests**
- [ ] **Step 5: Commit**

Suggested commit:

```bash
git add \
  src/myopenclaw/integrations/openviking/session_message_mapper.py \
  src/myopenclaw/integrations/openviking/commit_policy.py \
  tests/integrations/openviking/test_session_message_mapper.py \
  tests/integrations/openviking/test_commit_policy.py
git commit -m "feat: map session messages to openviking payloads"
```

### Task 6: Implement `OpenVikingSessionSync`

**Files:**
- Modify: `src/myopenclaw/integrations/openviking/session_sync.py`
- Test: `tests/integrations/openviking/test_openviking_session_sync.py`

- [ ] **Step 1: Write failing sync tests for watermark advancement and retry safety**
- [ ] **Step 2: Write failing commit tests for forced and policy-driven commit**
- [ ] **Step 3: Implement `OpenVikingSessionSync`**
- [ ] **Step 4: Run sync tests**
- [ ] **Step 5: Commit**

Suggested commit:

```bash
git add \
  src/myopenclaw/integrations/openviking/session_sync.py \
  tests/integrations/openviking/test_openviking_session_sync.py
git commit -m "feat: sync local sessions to openviking"
```

### Task 7: Rewire SessionService And AppAssembly

**Files:**
- Modify: `src/myopenclaw/conversations/service.py`
- Modify: `src/myopenclaw/app/assembly.py`
- Test: `tests/conversations/test_session_service.py`
- Test: `tests/app/test_assembly.py`

- [ ] **Step 1: Write failing service tests for sync-then-persist metadata ordering**
- [ ] **Step 2: Write failing assembly tests for noop vs real sync selection**
- [ ] **Step 3: Implement service ordering and assembly wiring**
- [ ] **Step 4: Run service and assembly tests**
- [ ] **Step 5: Commit**

Suggested commit:

```bash
git add \
  src/myopenclaw/conversations/service.py \
  src/myopenclaw/app/assembly.py \
  tests/conversations/test_session_service.py \
  tests/app/test_assembly.py
git commit -m "feat: wire openviking session sync into session service"
```

### Task 8: Verify Chat Loop Behavior And Regression Coverage

**Files:**
- Modify: `tests/cli/test_chat_loop.py`

- [ ] **Step 1: Add regression tests showing `start_index` still drives local append behavior**
- [ ] **Step 2: Add regression tests for close path committing remote state through the service**
- [ ] **Step 3: Run CLI chat loop tests**
- [ ] **Step 4: Commit**

Suggested commit:

```bash
git add tests/cli/test_chat_loop.py
git commit -m "test: cover chat loop session sync integration"
```

## 18. Full Verification Commands

Run focused suites during development:

```bash
PYTHONPATH=src uv run python -m unittest tests.conversations.test_session -v
PYTHONPATH=src uv run python -m unittest tests.conversations.test_session_storage_mapper -v
PYTHONPATH=src uv run python -m unittest tests.persistence.test_sqlite_session_repository -v
PYTHONPATH=src uv run python -m unittest tests.conversations.test_session_service -v
PYTHONPATH=src uv run python -m unittest tests.app.test_assembly -v
PYTHONPATH=src uv run python -m unittest tests.cli.test_chat_loop -v
PYTHONPATH=src uv run python -m unittest tests.integrations.openviking.test_session_message_mapper -v
PYTHONPATH=src uv run python -m unittest tests.integrations.openviking.test_commit_policy -v
PYTHONPATH=src uv run python -m unittest tests.integrations.openviking.test_openviking_session_sync -v
```

Run the full relevant set before handoff:

```bash
PYTHONPATH=src uv run python -m unittest \
  tests.conversations.test_session \
  tests.conversations.test_session_storage_mapper \
  tests.persistence.test_sqlite_session_repository \
  tests.conversations.test_session_service \
  tests.app.test_assembly \
  tests.cli.test_chat_loop \
  tests.integrations.openviking.test_session_message_mapper \
  tests.integrations.openviking.test_commit_policy \
  tests.integrations.openviking.test_openviking_session_sync -v
```

## 19. Rollout Notes

- This phase is safe to ship with OpenViking disabled by default.
- Enable it only when:
  - config is present
  - required env vars are set
  - manual verification against the production server succeeds
- Log remote sync failures clearly, but do not let them corrupt local SQLite state.
- If sync is enabled in production and failures appear:
  - local session recovery must continue to work
  - operators can inspect the sync watermarks in SQLite
  - the next turn should retry pending sync automatically

## 20. Acceptance Criteria

- After each successful turn, all locally persisted pending messages are synced to OpenViking.
- On a clean exit, all synced-but-uncommitted remote messages are committed.
- Local session recovery remains fully functional even if OpenViking is unavailable.
- Tool-batch assistant messages are exported as one assistant remote message with tool parts.
- Existing SQLite databases upgrade in place without manual migration steps.
- Session metadata persists:
  - remote session id
  - sync watermark
  - commit watermark
  - last remote commit time
  - bound OpenViking account/user/agent identity

