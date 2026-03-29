# ReAct Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a provider-neutral ReAct tool system with config-driven tool resolution, structured session messages, and a multi-step runtime loop.

**Architecture:** Introduce normalized tool and LLM IR types, resolve agent tools through a registry, and move tool orchestration into `AgentRuntime`. Keep `Session` as model-visible state only, while `GeminiProvider` maps internal tool semantics to Gemini function calling.

**Tech Stack:** Python 3.12, dataclasses, Pydantic, Typer, Rich, google-genai, unittest

---

### Task 1: Add tool abstractions and registry

**Files:**
- Modify: `src/myopenclaw/tools/base.py`
- Create: `src/myopenclaw/tools/registry.py`
- Create: `src/myopenclaw/tools/builtin.py`
- Test: `tests/tools/test_registry.py`

- [ ] **Step 1: Write the failing test**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement normalized tool types, execution context/result, and registry-backed tool resolution**
- [ ] **Step 4: Run test to verify it passes**

### Task 2: Upgrade agent/config wiring and structured message model

**Files:**
- Modify: `src/myopenclaw/agent/definition.py`
- Modify: `src/myopenclaw/agent/agent.py`
- Modify: `src/myopenclaw/config/app_config.py`
- Modify: `src/myopenclaw/conversation/message.py`
- Modify: `src/myopenclaw/conversation/session.py`
- Modify: `src/myopenclaw/conversation/__init__.py`
- Test: `tests/config/test_app_config.py`
- Test: `tests/conversation/test_session.py`

- [ ] **Step 1: Write the failing test**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement config-driven tool ids and structured session messages for tool calls/results**
- [ ] **Step 4: Run test to verify it passes**

### Task 3: Upgrade provider IR and ReAct runtime loop

**Files:**
- Modify: `src/myopenclaw/llm/chat_types.py`
- Modify: `src/myopenclaw/llm/provider.py`
- Modify: `src/myopenclaw/llm/providers/gemini.py`
- Modify: `src/myopenclaw/llm/__init__.py`
- Modify: `src/myopenclaw/agent/runtime.py`
- Modify: `src/myopenclaw/interfaces/cli/chat.py`
- Modify: `config.yaml`
- Test: `tests/agent/test_runtime.py`
- Test: `tests/interfaces/cli/test_chat_loop.py`
- Test: `tests/llm/providers/test_gemini.py`

- [ ] **Step 1: Write the failing test**
- [ ] **Step 2: Run test to verify it fails**
- [ ] **Step 3: Implement multi-step tool loop and Gemini tool-calling translation**
- [ ] **Step 4: Run targeted tests and package verification**
