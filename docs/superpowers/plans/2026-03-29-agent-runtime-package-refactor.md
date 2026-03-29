# Agent Runtime Package Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reshape the codebase so `Agent` is a declarative core entity, runtime execution moves into a dedicated `runtime` package, and session creation belongs to the conversation layer instead of the agent layer.

**Architecture:** Replace the current mixed `agent` package with a cleaner split: `agent` holds the core agent model, `runtime` holds turn execution and runtime events, and `conversation` owns session lifecycle helpers. Runtime resolves provider and tools from the Agent's declared capabilities instead of the Agent holding bound runtime objects.

**Tech Stack:** Python 3.12, dataclasses, Pydantic, Typer, Rich, google-genai, unittest

---

### Task 1: Add failing tests for the new package boundaries

**Files:**
- Create: `tests/runtime/__init__.py`
- Create: `tests/runtime/test_runner.py`
- Create: `tests/runtime/test_events.py`
- Modify: `tests/interfaces/cli/test_chat_loop.py`
- Modify: `tests/agent/test_session_factory.py`

- [ ] **Step 1: Write the failing tests that import runtime types from `myopenclaw.runtime` and create sessions from `conversation.Session`**
- [ ] **Step 2: Run the targeted tests and verify they fail because the runtime package and session API do not exist yet**
- [ ] **Step 3: Implement the minimal package moves and APIs needed to satisfy the tests**
- [ ] **Step 4: Re-run the targeted tests and verify they pass**

### Task 2: Make Agent a declarative core entity

**Files:**
- Modify: `src/myopenclaw/agent/agent.py`
- Modify: `src/myopenclaw/app/assembly.py`
- Modify: `src/myopenclaw/app/bootstrap.py`
- Modify: `tests/agent/test_runtime.py`
- Modify: `tests/agent/test_events.py`
- Modify: `tests/app/test_assembly.py`

- [ ] **Step 1: Write the failing tests for an Agent that owns behavior, default brain config, workspace, and tool ids directly**
- [ ] **Step 2: Run the targeted tests and verify they fail**
- [ ] **Step 3: Implement the minimal Agent model change and update assembly/bootstrap to build it**
- [ ] **Step 4: Re-run the targeted tests and verify they pass**

### Task 3: Move runtime execution into a dedicated package

**Files:**
- Create: `src/myopenclaw/runtime/__init__.py`
- Create: `src/myopenclaw/runtime/events.py`
- Create: `src/myopenclaw/runtime/runner.py`
- Modify: `src/myopenclaw/interfaces/cli/chat.py`
- Modify: `src/myopenclaw/interfaces/cli/event_renderer.py`
- Modify: `tests/runtime/test_runner.py`
- Modify: `tests/runtime/test_events.py`

- [ ] **Step 1: Write the failing tests for `TurnRunner` and runtime events under `myopenclaw.runtime`**
- [ ] **Step 2: Run the targeted tests and verify they fail**
- [ ] **Step 3: Implement the runtime package and rewire CLI imports**
- [ ] **Step 4: Re-run the targeted tests and verify they pass**

### Task 4: Move session creation to the conversation layer

**Files:**
- Modify: `src/myopenclaw/conversation/session.py`
- Delete: `src/myopenclaw/agent/session_factory.py`
- Modify: `src/myopenclaw/conversation/__init__.py`
- Modify: `src/myopenclaw/interfaces/cli/chat.py`
- Modify: `tests/agent/test_session_factory.py`

- [ ] **Step 1: Write the failing tests for session creation from `Session` itself**
- [ ] **Step 2: Run the targeted tests and verify they fail**
- [ ] **Step 3: Implement the minimal session factory move and remove the old agent-level factory**
- [ ] **Step 4: Re-run the targeted tests and verify they pass**

### Task 5: Verify the full system still works

**Files:**
- Test: `tests/runtime/test_runner.py`
- Test: `tests/runtime/test_events.py`
- Test: `tests/interfaces/cli/test_chat_loop.py`
- Test: `tests/app/test_assembly.py`
- Test: `tests/conversation/test_session.py`
- Test: `tests/llm/providers/test_gemini.py`

- [ ] **Step 1: Run the targeted tests for each changed package boundary**
- [ ] **Step 2: Run the full test suite**
- [ ] **Step 3: Fix any regressions without broadening scope**
