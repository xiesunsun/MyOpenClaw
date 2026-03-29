# Package Architecture Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tighten package boundaries around agent runtime, app assembly, generation protocol types, and default tool provisioning without changing chat-loop behavior.

**Architecture:** Keep `conversation` as the stable transcript core. Split orchestration concerns into focused agent modules, move assembly logic out of `AppConfig`, introduce a neutral generation protocol package consumed by runtime and providers, and separate tool catalog/provisioning from the registry type.

**Tech Stack:** Python 3.12, dataclasses, Pydantic, Typer, Rich, google-genai, unittest

---

### Task 1: Add tests for assembly and tool provisioning boundaries

**Files:**
- Create: `tests/app/test_assembly.py`
- Modify: `tests/tools/test_registry.py`
- Modify: `tests/config/test_app_config.py`

- [ ] **Step 1: Write the failing tests**
- [ ] **Step 2: Run the targeted tests and verify they fail for the expected missing interfaces**
- [ ] **Step 3: Implement the minimal code needed for the new assembly and provisioning boundary**
- [ ] **Step 4: Re-run the targeted tests and verify they pass**

### Task 2: Extract generation protocol types from `llm`

**Files:**
- Create: `src/myopenclaw/runtime_protocols/__init__.py`
- Create: `src/myopenclaw/runtime_protocols/generation.py`
- Modify: `src/myopenclaw/agent/runtime.py`
- Modify: `src/myopenclaw/llm/provider.py`
- Modify: `src/myopenclaw/llm/providers/gemini.py`
- Modify: `src/myopenclaw/llm/__init__.py`
- Modify: `tests/agent/test_runtime.py`
- Modify: `tests/interfaces/cli/test_chat_loop.py`
- Modify: `tests/llm/providers/test_gemini.py`

- [ ] **Step 1: Write the failing import and behavior tests**
- [ ] **Step 2: Run the targeted tests and verify they fail because the protocol package does not exist yet**
- [ ] **Step 3: Implement the protocol extraction with the smallest possible API surface**
- [ ] **Step 4: Re-run the targeted tests and verify they pass**

### Task 3: Split agent definition, runtime, and behavior loading more cleanly

**Files:**
- Create: `src/myopenclaw/agent/session_factory.py`
- Modify: `src/myopenclaw/agent/agent.py`
- Modify: `src/myopenclaw/agent/runtime.py`
- Modify: `src/myopenclaw/agent/__init__.py`
- Modify: `tests/agent/test_runtime.py`

- [ ] **Step 1: Write the failing tests for the new focused responsibilities**
- [ ] **Step 2: Run the targeted tests and verify they fail**
- [ ] **Step 3: Move session creation responsibility out of the mutable agent object and keep runtime orchestration intact**
- [ ] **Step 4: Re-run the targeted tests and verify they pass**

### Task 4: Move agent definition assembly out of `AppConfig`

**Files:**
- Create: `src/myopenclaw/app/assembly.py`
- Modify: `src/myopenclaw/app/bootstrap.py`
- Modify: `src/myopenclaw/config/app_config.py`
- Modify: `tests/app/test_assembly.py`
- Modify: `tests/config/test_app_config.py`

- [ ] **Step 1: Write the failing tests for external agent definition assembly**
- [ ] **Step 2: Run the targeted tests and verify they fail**
- [ ] **Step 3: Implement an app assembly service and reduce `AppConfig` back to config resolution**
- [ ] **Step 4: Re-run the targeted tests and verify they pass**

### Task 5: Verify the full runtime still works

**Files:**
- Test: `tests/agent/test_runtime.py`
- Test: `tests/interfaces/cli/test_chat_loop.py`
- Test: `tests/llm/providers/test_gemini.py`
- Test: `tests/config/test_app_config.py`
- Test: `tests/tools/test_registry.py`
- Test: `tests/conversation/test_session.py`

- [ ] **Step 1: Run the targeted tests for each refactored boundary**
- [ ] **Step 2: Run the full test suite**
- [ ] **Step 3: Fix any regressions without broadening scope**
