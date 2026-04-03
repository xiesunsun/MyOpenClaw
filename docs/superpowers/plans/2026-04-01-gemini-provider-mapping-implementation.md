# Gemini Provider Mapping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve the existing Gemini provider mapping so current ReAct and tool-calling behavior is clearer, more faithful to the SDK contract, and easier to observe without adding new product capabilities.

**Architecture:** Keep the current `ReActStrategy -> GeminiProvider -> Session` flow intact. Tighten the Gemini-specific translation layer by improving tool response payload semantics, exposing tool output schema in Gemini function declarations, and preserving richer response metadata inside the existing runtime result objects.

**Tech Stack:** Python, google-genai, dataclasses, unittest

---

### Task 1: Tighten Gemini tool declaration and tool-response mappings

**Files:**
- Modify: `src/myopenclaw/llm/providers/gemini.py`
- Modify: `tests/llm/providers/test_gemini.py`

- [ ] **Step 1: Update Gemini tool declarations to pass through `ToolSpec.output_schema` when present**
- [ ] **Step 2: Change Gemini tool result packaging to emit clearer success and error semantics while preserving metadata**
- [ ] **Step 3: Extend provider tests to lock down the new declaration and tool-response payload shapes**

### Task 2: Preserve richer Gemini response metadata in the current runtime model

**Files:**
- Modify: `src/myopenclaw/runtime/generation.py`
- Modify: `src/myopenclaw/llm/providers/gemini.py`
- Modify: `tests/llm/providers/test_gemini.py`

- [ ] **Step 1: Extend the runtime usage model with optional Gemini-specific token counters and finish-reason details**
- [ ] **Step 2: Populate those fields from the Gemini response without changing the existing ReAct control flow**
- [ ] **Step 3: Add tests for the expanded extraction behavior**

### Task 3: Run focused regression coverage

**Files:**
- Test: `tests/llm/providers/test_gemini.py`
- Test: `tests/runtime/test_generation.py`
- Test: `tests/runtime/test_runner.py`
- Test: `tests/runtime/test_events.py`

- [ ] **Step 1: Run the provider and runtime unit tests covering generation and tool-call persistence**
- [ ] **Step 2: Fix any regressions without broadening scope**
- [ ] **Step 3: Re-run the focused verification set and record the results**
