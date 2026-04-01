# Persistent Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current one-shot `bash` tool with a single-shell-per-session persistent shell implementation that supports `shell_exec`, `shell_restart`, `shell_close`, and structured shell state metadata.

**Architecture:** Keep the existing `TurnRunner -> ToolExecutionContext -> BaseTool` flow. Replace the current stateless shell executor with a PTY-backed `PersistentShell`, managed by `ShellSessionManager`, and expose it through three focused tools. Feed structured shell metadata back through `Session` and the provider tool-response payload.

**Tech Stack:** Python 3.12, dataclasses, `pty`, `os`, `select`, `subprocess`, unittest

---

### Task 1: Replace shell contract tests

**Files:**
- Modify: `tests/tools/test_shell.py`
- Modify: `tests/tools/test_builtin.py`
- Modify: `tests/runtime/test_runner.py`

- [ ] **Step 1: Write failing tests for `shell_exec`, `shell_restart`, `shell_close`, and manager lifecycle**
- [ ] **Step 2: Run the targeted shell and runtime tests to verify they fail for the missing API**
- [ ] **Step 3: Update existing `bash`-based assertions to the new single-shell contract**
- [ ] **Step 4: Re-run targeted tests and confirm the failures are now about missing implementation, not stale expectations**

### Task 2: Implement persistent shell runtime

**Files:**
- Modify: `src/myopenclaw/tools/shell.py`
- Test: `tests/tools/test_shell.py`

- [ ] **Step 1: Add `ShellStatus`, `ShellExecutionResult`, `PersistentShell`, and PTY-backed process wrapper**
- [ ] **Step 2: Implement marker-based command completion with `exit_code` and `cwd` parsing**
- [ ] **Step 3: Implement interrupt/terminate/restart behavior and single-command-at-a-time protection**
- [ ] **Step 4: Re-run targeted shell tests and fix behavior gaps**

### Task 3: Expose the new shell tools

**Files:**
- Modify: `src/myopenclaw/tools/shell.py`
- Modify: `src/myopenclaw/tools/catalog.py`
- Modify: `config.yaml`
- Test: `tests/tools/test_builtin.py`

- [ ] **Step 1: Add `ShellExecTool`, `ShellRestartTool`, and `ShellCloseTool` with minimal schemas**
- [ ] **Step 2: Register the new tools in the builtin catalog and update configured agent tool ids**
- [ ] **Step 3: Re-run targeted builtin tests and adjust any catalog/config expectations**

### Task 4: Return structured shell metadata to the model

**Files:**
- Modify: `src/myopenclaw/llm/providers/gemini.py`
- Modify: `tests/runtime/test_runner.py`
- Modify: `tests/llm/providers/test_gemini.py`

- [ ] **Step 1: Add failing tests for tool-response payloads including metadata**
- [ ] **Step 2: Update provider serialization to include tool result metadata in function responses**
- [ ] **Step 3: Re-run targeted runtime/provider tests and verify metadata now flows end-to-end**

### Task 5: Verify the integrated stack

**Files:**
- Test: `tests/tools/test_shell.py`
- Test: `tests/tools/test_builtin.py`
- Test: `tests/runtime/test_runner.py`
- Test: `tests/llm/providers/test_gemini.py`
- Test: `tests`

- [ ] **Step 1: Run the targeted verification set**
- [ ] **Step 2: Fix any regressions without broadening scope**
- [ ] **Step 3: Run the full unittest suite and record the final result**
