# Tool Batch Parallel Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add step-scoped tool call batches so one model step can execute multiple tool calls concurrently while preserving call-order session history, Gemini serialization order, and CLI progress visibility.

**Architecture:** Keep the current `AgentCoordinator -> ReActStrategy -> GeminiProvider -> Session` flow, but replace scattered tool result messages with an assistant-owned `ToolCallBatch`. Runtime gathers concurrent tool outcomes, emits batch-aware events, and persists one ordered batch snapshot after all tool calls finish. Provider and CLI consume that structured batch rather than reconstructing relationships from implicit ordering.

**Tech Stack:** Python 3.12, dataclasses, asyncio task orchestration, Rich CLI rendering, google-genai provider mapping, unittest/pytest via uv.

---

### Task 1: Introduce Batch-Oriented Conversation Models

**Files:**
- Modify: `src/myopenclaw/conversation/message.py`
- Modify: `src/myopenclaw/conversation/session.py`
- Test: `tests/conversation/test_session.py`

- [ ] **Step 1: Write failing session tests for assistant tool batches**
- [ ] **Step 2: Run `uv run pytest tests/conversation/test_session.py -v` and confirm the old shape fails**
- [ ] **Step 3: Add `ToolCallResult` and `ToolCallBatch`, replace scattered tool-result fields with `SessionMessage.tool_call_batch`, and add `Session.append_assistant_tool_batch(...)`**
- [ ] **Step 4: Re-run `uv run pytest tests/conversation/test_session.py -v` and confirm the new batch persistence passes**

### Task 2: Convert Gemini Provider Mapping to Read Structured Batches

**Files:**
- Modify: `src/myopenclaw/llm/providers/gemini.py`
- Test: `tests/llm/providers/test_gemini.py`

- [ ] **Step 1: Write failing provider tests covering one assistant tool batch mapping to ordered Gemini function calls and function responses**
- [ ] **Step 2: Run `uv run pytest tests/llm/providers/test_gemini.py -v` and confirm the old TOOL-message mapping fails**
- [ ] **Step 3: Update `_build_contents()` to serialize `SessionMessage.tool_call_batch` into one model content plus one ordered function-response content**
- [ ] **Step 4: Re-run `uv run pytest tests/llm/providers/test_gemini.py -v` and confirm ordered batch mapping passes**

### Task 3: Make ReAct Runtime Execute Tool Calls Concurrently and Persist Ordered Batches

**Files:**
- Modify: `src/myopenclaw/runtime/events.py`
- Modify: `src/myopenclaw/runtime/strategy/react.py`
- Test: `tests/runtime/test_events.py`
- Test: `tests/runtime/test_runner.py`

- [ ] **Step 1: Write failing runtime tests for concurrent tool execution, ordered batch persistence, and batch-aware runtime events**
- [ ] **Step 2: Run `uv run pytest tests/runtime/test_events.py tests/runtime/test_runner.py -v` and confirm the serial TOOL-message behavior fails**
- [ ] **Step 3: Add `ToolCallOutcome`, batch-aware event metadata, concurrent task gathering, and `append_assistant_tool_batch(...)` persistence in call order**
- [ ] **Step 4: Re-run `uv run pytest tests/runtime/test_events.py tests/runtime/test_runner.py -v` and confirm batch order and event payloads pass**

### Task 4: Render Batch Progress in the CLI

**Files:**
- Modify: `src/myopenclaw/interfaces/cli/event_renderer.py`
- Modify: `src/myopenclaw/interfaces/cli/chat.py`
- Test: `tests/interfaces/cli/test_chat_loop.py`

- [ ] **Step 1: Write failing CLI tests for batch-oriented progress rendering and session replay from assistant tool batches**
- [ ] **Step 2: Run `uv run pytest tests/interfaces/cli/test_chat_loop.py -v` and confirm the old panel-per-event assumptions fail**
- [ ] **Step 3: Add renderer batch state, map completed vs failed events onto stable rows, and replay assistant tool batches from session history**
- [ ] **Step 4: Re-run `uv run pytest tests/interfaces/cli/test_chat_loop.py -v` and confirm batch display behavior passes**

### Task 5: Run Targeted Verification

**Files:**
- Test: `tests/conversation/test_session.py`
- Test: `tests/llm/providers/test_gemini.py`
- Test: `tests/runtime/test_events.py`
- Test: `tests/runtime/test_runner.py`
- Test: `tests/interfaces/cli/test_chat_loop.py`

- [ ] **Step 1: Run `uv run pytest tests/conversation/test_session.py tests/llm/providers/test_gemini.py tests/runtime/test_events.py tests/runtime/test_runner.py tests/interfaces/cli/test_chat_loop.py -v`**
- [ ] **Step 2: Run `uv run python -m compileall src/myopenclaw`**
- [ ] **Step 3: Fix any regressions uncovered by the targeted suite**
