# Agent Session Runtime Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor conversation and related runtime code so `Session` is a persistent model-visible transcript, while turn execution moves into a dedicated runtime service.

**Architecture:** Keep `Agent` as the static definition holder for workspace, behavior, model, and tools. Replace the current session/state mix with a single persistent `Session` entity plus `SessionMessage`, and introduce a runtime service that executes one conversational turn using `Agent + Session + Provider`.

**Tech Stack:** Python 3.12, dataclasses, Pydantic, Typer, Rich, google-genai, unittest

---

### Task 1: Define the new conversation model

**Files:**
- Modify: `src/myopenclaw/conversation/message.py`
- Modify: `src/myopenclaw/conversation/session.py`
- Modify: `src/myopenclaw/conversation/__init__.py`
- Delete: `src/myopenclaw/conversation/state.py`
- Test: `tests/conversation/test_session.py`

- [ ] **Step 1: Write the failing test**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement `Session` and `SessionMessage` as persistent conversation entities**
- [ ] **Step 4: Run test to verify it passes**

### Task 2: Move turn execution into runtime

**Files:**
- Create: `src/myopenclaw/agent/runtime.py`
- Modify: `src/myopenclaw/agent/agent.py`
- Modify: `src/myopenclaw/llm/chat_types.py`
- Modify: `src/myopenclaw/llm/provider.py`
- Modify: `src/myopenclaw/llm/providers/gemini.py`
- Modify: `src/myopenclaw/llm/__init__.py`
- Test: `tests/agent/test_runtime.py`

- [ ] **Step 1: Write the failing test**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement runtime orchestration and provider API rename**
- [ ] **Step 4: Run test to verify it passes**

### Task 3: Rewire bootstrap and CLI to the new boundary

**Files:**
- Modify: `src/myopenclaw/app/bootstrap.py`
- Modify: `src/myopenclaw/interfaces/cli/chat.py`
- Test: `tests/interfaces/cli/test_chat_loop.py`

- [ ] **Step 1: Write the failing test**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Update bootstrap and chat loop to use `Session` + runtime**
- [ ] **Step 4: Run targeted tests and package compilation**

