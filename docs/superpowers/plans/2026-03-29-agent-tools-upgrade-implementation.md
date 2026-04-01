# Agent Builtin Tools Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement production-usable builtin `read`, `write`, and `bash` tools with workspace-aware filesystem boundaries, shell session support, and persisted tool result metadata.

**Architecture:** Keep the existing `ToolRegistry -> TurnRunner -> Provider` flow intact. Extend the base tool contract just enough to inject `PathAccessPolicy` and `ShellSessionManager`, persist tool result metadata in conversation state, and add concrete filesystem and shell tool modules without introducing extra service-container abstractions.

**Tech Stack:** Python, dataclasses, pathlib, subprocess, unittest, Rich

---

### Task 1: Extend the shared tool and session contracts

**Files:**
- Modify: `src/myopenclaw/tools/base.py`
- Modify: `src/myopenclaw/conversation/message.py`
- Modify: `src/myopenclaw/conversation/session.py`
- Modify: `tests/tools/test_base.py`
- Modify: `tests/conversation/test_session.py`

- [ ] **Step 1: Write failing tests for `ToolExecutionContext` carrying `path_policy` and `shell_session_manager`, and for `Session` persisting tool result metadata**
- [ ] **Step 2: Run the targeted tests and verify they fail for the expected missing fields/signatures**
- [ ] **Step 3: Implement the minimal contract changes in base tool types and conversation entities**
- [ ] **Step 4: Re-run the targeted tests and verify they pass**

### Task 2: Add workspace-aware filesystem tools

**Files:**
- Create: `src/myopenclaw/tools/policy.py`
- Create: `src/myopenclaw/tools/filesystem.py`
- Modify: `src/myopenclaw/tools/catalog.py`
- Modify: `tests/tools/test_builtin.py`
- Create: `tests/tools/test_filesystem.py`

- [ ] **Step 1: Write failing tests for workspace path resolution, bounded `read`, and action-based `write`**
- [ ] **Step 2: Run the targeted tests and verify they fail for the missing policy and tool classes**
- [ ] **Step 3: Implement `PathAccessPolicy`, `WorkspacePathAccessPolicy`, `ReadTool`, and `WriteTool` with minimal action support**
- [ ] **Step 4: Re-run the targeted tests and verify they pass**

### Task 3: Add shell execution support and `bash` tool

**Files:**
- Create: `src/myopenclaw/tools/shell.py`
- Modify: `src/myopenclaw/tools/catalog.py`
- Modify: `tests/runtime/test_runner.py`
- Create: `tests/tools/test_shell.py`

- [ ] **Step 1: Write failing tests for shell session persistence, command execution metadata, and `bash` tool registration**
- [ ] **Step 2: Run the targeted tests and verify they fail for the missing shell classes**
- [ ] **Step 3: Implement `ShellCommandResult`, `ShellSession`, `ShellExecutor`, `SubprocessShellExecutor`, `ShellSessionManager`, and `BashTool`**
- [ ] **Step 4: Re-run the targeted tests and verify they pass**

### Task 4: Rewire runtime and CLI-facing persistence

**Files:**
- Modify: `src/myopenclaw/runtime/runner.py`
- Modify: `src/myopenclaw/interfaces/cli/chat.py`
- Modify: `src/myopenclaw/interfaces/cli/event_renderer.py`
- Modify: `tests/interfaces/cli/test_chat_loop.py`
- Modify: `tests/runtime/test_events.py`

- [ ] **Step 1: Write failing tests for runtime-created tool execution context and metadata persistence into `SessionMessage`**
- [ ] **Step 2: Run the targeted tests and verify they fail for the missing runtime wiring**
- [ ] **Step 3: Implement the minimal runtime changes and optional CLI metadata rendering adjustments**
- [ ] **Step 4: Re-run the targeted tests and verify they pass**

### Task 5: Verify the integrated tool stack

**Files:**
- Test: `tests/tools/test_base.py`
- Test: `tests/conversation/test_session.py`
- Test: `tests/tools/test_filesystem.py`
- Test: `tests/tools/test_shell.py`
- Test: `tests/tools/test_builtin.py`
- Test: `tests/runtime/test_runner.py`
- Test: `tests/interfaces/cli/test_chat_loop.py`

- [ ] **Step 1: Run the full targeted verification set**
- [ ] **Step 2: Fix any regressions without broadening scope**
- [ ] **Step 3: Re-run verification and capture the final evidence**
