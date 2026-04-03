# Strict Layering Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor MyOpenClaw into strict `shared/domain/application/infrastructure/interfaces/bootstrap` layers with one-way dependencies and no CLI or infrastructure leakage into core orchestration.

**Architecture:** Introduce explicit target-layer packages and move contracts to their owning layer before rewiring runtime flow. Application will own ports, DTOs, and turn orchestration; infrastructure will implement adapters; interfaces will consume only application-facing services and events; bootstrap will become the sole composition root.

**Tech Stack:** Python 3, dataclasses, Pydantic, Typer, Rich, Google Gemini SDK, pytest

---

### Task 1: Create Target Layer Skeleton

**Files:**
- Create: `src/myopenclaw/domain/__init__.py`
- Create: `src/myopenclaw/domain/agent.py`
- Create: `src/myopenclaw/domain/session.py`
- Create: `src/myopenclaw/domain/message.py`
- Create: `src/myopenclaw/domain/metadata.py`
- Create: `src/myopenclaw/application/__init__.py`
- Create: `src/myopenclaw/application/ports.py`
- Create: `src/myopenclaw/application/contracts.py`
- Create: `src/myopenclaw/application/events.py`
- Create: `src/myopenclaw/application/services.py`
- Create: `src/myopenclaw/infrastructure/__init__.py`
- Create: `src/myopenclaw/infrastructure/providers/__init__.py`
- Create: `src/myopenclaw/infrastructure/tools/__init__.py`
- Create: `src/myopenclaw/infrastructure/config/__init__.py`
- Create: `src/myopenclaw/bootstrap/__init__.py`
- Create: `src/myopenclaw/interfaces/__init__.py`

- [ ] Define the new package skeleton without deleting old packages.
- [ ] Export only layer-owned symbols from each new package.
- [ ] Keep old imports working temporarily via compatibility re-exports only where needed during migration.

### Task 2: Move Domain State Into `domain`

**Files:**
- Create: `src/myopenclaw/domain/message.py`
- Create: `src/myopenclaw/domain/metadata.py`
- Create: `src/myopenclaw/domain/session.py`
- Create: `src/myopenclaw/domain/agent.py`
- Modify: `src/myopenclaw/conversations/message.py`
- Modify: `src/myopenclaw/conversations/metadata.py`
- Modify: `src/myopenclaw/conversations/session.py`
- Modify: `src/myopenclaw/agents/agent.py`

- [ ] Move `Session`, `SessionMessage`, `ToolCall`, `ToolCallBatch`, and `MessageMetadata` into `domain`.
- [ ] Move `Agent` into `domain` and remove IO-specific concerns that do not belong in the entity.
- [ ] Turn old `agents` and `conversations` modules into compatibility shims during migration.

### Task 3: Move Application Contracts and Ports Into `application`

**Files:**
- Create: `src/myopenclaw/application/contracts.py`
- Create: `src/myopenclaw/application/ports.py`
- Create: `src/myopenclaw/application/events.py`
- Modify: `src/myopenclaw/shared/generation.py`
- Modify: `src/myopenclaw/providers/base.py`
- Modify: `src/myopenclaw/tools/base.py`
- Modify: `src/myopenclaw/runs/events.py`

- [ ] Move `GenerateRequest`, `GenerateResult`, `FinishReason`, and `TokenUsage` out of `shared`.
- [ ] Define `LLMPort` and `ToolExecutorPort` in `application`.
- [ ] Replace event payloads that expose tool/domain implementation types with application-owned DTOs.
- [ ] Preserve adapter inputs/outputs by translating at the layer boundary instead of using `Any`.

### Task 4: Rebuild Orchestration In `application`

**Files:**
- Create: `src/myopenclaw/application/services.py`
- Modify: `src/myopenclaw/runs/coordinator.py`
- Modify: `src/myopenclaw/runs/strategy/base.py`
- Modify: `src/myopenclaw/runs/strategy/react.py`
- Modify: `src/myopenclaw/runs/context.py`
- Modify: `src/myopenclaw/runs/__init__.py`

- [ ] Move turn orchestration into application services that depend only on `domain`, `shared`, and application ports.
- [ ] Remove provider, tool registry, file service, and shell session construction from application runtime context.
- [ ] Make application accept injected dependencies and emit application-neutral runtime events and turn results.

### Task 5: Rebuild Infrastructure Adapters

**Files:**
- Create: `src/myopenclaw/infrastructure/providers/gemini.py`
- Create: `src/myopenclaw/infrastructure/providers/tool_executor.py`
- Create: `src/myopenclaw/infrastructure/tools/catalog.py`
- Create: `src/myopenclaw/infrastructure/tools/registry.py`
- Create: `src/myopenclaw/infrastructure/config/app_config.py`
- Create: `src/myopenclaw/infrastructure/agents/behavior_loader.py`
- Modify: `src/myopenclaw/providers/gemini.py`
- Modify: `src/myopenclaw/providers/factory.py`
- Modify: `src/myopenclaw/tools/catalog.py`
- Modify: `src/myopenclaw/tools/registry.py`
- Modify: `src/myopenclaw/config/app_config.py`
- Modify: `src/myopenclaw/agents/behavior_loader.py`

- [ ] Make Gemini implement `LLMPort` using application DTOs only.
- [ ] Introduce a tool-executor adapter that hides concrete tool classes from application.
- [ ] Move YAML config loading and behavior file loading under infrastructure.
- [ ] Keep old package entry points as thin shims until call sites migrate.

### Task 6: Move Composition To `bootstrap`

**Files:**
- Create: `src/myopenclaw/bootstrap/assembly.py`
- Modify: `src/myopenclaw/app/assembly.py`
- Modify: `src/myopenclaw/providers/factory.py`
- Modify: `src/myopenclaw/runs/context.py`

- [ ] Make bootstrap the only place that wires config, behavior, agent construction, ports, and CLI runtime dependencies.
- [ ] Remove all concrete assembly logic from application and interfaces.
- [ ] Keep `app` as a temporary compatibility alias to bootstrap if needed.

### Task 7: Restrict CLI To `interfaces`

**Files:**
- Create: `src/myopenclaw/interfaces/cli/chat.py`
- Create: `src/myopenclaw/interfaces/cli/event_renderer.py`
- Create: `src/myopenclaw/interfaces/cli/main.py`
- Modify: `src/myopenclaw/cli/chat.py`
- Modify: `src/myopenclaw/cli/event_renderer.py`
- Modify: `src/myopenclaw/cli/main.py`

- [ ] Make CLI depend only on application-facing services, DTOs, and runtime events.
- [ ] Remove direct imports of `Session`, `ToolCallBatch`, `AppAssembly`, and provider/tool implementation types from CLI.
- [ ] Keep CLI behavior unchanged from the user’s perspective.

### Task 8: Tighten Checks and Tests

**Files:**
- Modify: `scripts/check_layer_dependencies.py`
- Modify: `tests/lint/test_layer_dependencies.py`
- Modify: `tests/cli/test_chat_loop.py`
- Modify: `tests/runs/test_runner.py`
- Modify: `tests/providers/test_gemini.py`
- Modify: `tests/tools/test_registry.py`

- [ ] Replace old package dependency rules with target-layer rules.
- [ ] Update tests to assert application contracts instead of old cross-layer objects.
- [ ] Run targeted tests after each major boundary move.
- [ ] Remove compatibility re-exports once all imports follow target layering.
