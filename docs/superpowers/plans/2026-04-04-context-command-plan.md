# Context Command Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/context` chat command that shows the current resident context state before the next user message is sent, including total usage, system prompt, skills, tools, messages, and free space.

**Architecture:** Use the last completed model-step metadata as the source of truth for current total occupied context. Estimate `System prompt`, `Skills`, and `Tools` separately, then derive `Messages` as the residual: `total - system - skills - tools`. Keep the rendering logic separate from the computation logic so later context compaction can reuse the same snapshot model.

**Tech Stack:** Python 3.12, Rich, Typer, google-genai provider integration, unittest

---

## File Structure

- Modify: `src/myopenclaw/shared/model_config.py`
  Add `max_input_tokens` to the model config surface so `/context` can compute free space from configured model limits.

- Modify: `src/myopenclaw/agents/agent.py`
  Expose separated instruction parts so `System prompt` and `Skills` can be estimated independently without re-parsing a composed string.

- Modify: `src/myopenclaw/agents/skills.py`
  Add a helper that returns the skill guidance block and the formatted skill catalog as distinct text segments.

- Modify: `src/myopenclaw/providers/base.py`
  Add a provider capability for token estimation of context fragments used by `/context`.

- Modify: `src/myopenclaw/providers/gemini.py`
  Implement token estimation helpers for text and tool declarations using Gemini’s official token counting path.

- Create: `src/myopenclaw/runs/context_usage.py`
  Define the context snapshot dataclasses and the service that computes total usage, category usage, free space, and optional drill-down details.

- Create: `src/myopenclaw/cli/context_renderer.py`
  Render the `/context` output using Rich, including the grid/bar visualization, category list, and skills breakdown section.

- Modify: `src/myopenclaw/cli/chat.py`
  Add `/context` to the available commands, wire the command to the new service and renderer, and keep command handling in the chat loop.

- Modify: `tests/config/test_app_config.py`
  Verify `max_input_tokens` is loaded from config.

- Modify: `tests/providers/test_gemini.py`
  Verify Gemini token estimation request building for text and tool declarations.

- Create: `tests/runs/test_context_usage.py`
  Verify context snapshot computation, residual message calculation, free-space calculation, and missing-metadata fallbacks.

- Modify: `tests/cli/test_chat_loop.py`
  Verify `/context` is listed in help and prints a context usage panel.

### Task 1: Surface Model Input Limits

**Files:**
- Modify: `src/myopenclaw/shared/model_config.py`
- Modify: `tests/config/test_app_config.py`

- [ ] **Step 1: Write the failing config test for `max_input_tokens`**

```python
def test_resolve_model_config_includes_max_input_tokens(self) -> None:
    config = AppConfig.load(config_path)
    model_config = config.resolve_model_config()
    self.assertEqual(1048576, model_config.max_input_tokens)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/config/test_app_config.py -k max_input_tokens -v`
Expected: FAIL because `ModelConfig` does not expose `max_input_tokens`.

- [ ] **Step 3: Add `max_input_tokens` to the base model config**

```python
class BaseModelConfig(BaseModel):
    api_key: str | None = None
    api_base: str | None = None
    temperature: float = 1.0
    max_input_tokens: int | None = None
    max_output_tokens: int = 65536
```

- [ ] **Step 4: Run the config tests to verify they pass**

Run: `python3 -m pytest tests/config/test_app_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/myopenclaw/shared/model_config.py tests/config/test_app_config.py
git commit -m "feat: surface model input token limits"
```

### Task 2: Split Agent Instruction Parts

**Files:**
- Modify: `src/myopenclaw/agents/skills.py`
- Modify: `src/myopenclaw/agents/agent.py`
- Test: `tests/agents/test_skills.py`

- [ ] **Step 1: Write the failing test for separated instruction parts**

```python
def test_compose_system_instruction_parts_separates_behavior_and_skills(self) -> None:
    parts = compose_system_instruction_parts("You are Pickle.", [skill])
    assert parts.base_instruction == "You are Pickle."
    assert "Available skills:" in parts.skills_catalog
    assert "filesystem-based skills" in parts.skills_guidance
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/agents/test_skills.py -k instruction_parts -v`
Expected: FAIL because the helper does not exist.

- [ ] **Step 3: Implement instruction part helpers**

```python
@dataclass(frozen=True)
class SystemInstructionParts:
    base_instruction: str
    skills_guidance: str
    skills_catalog: str

    @property
    def full_instruction(self) -> str:
        return "\n\n".join(
            section for section in [
                self.base_instruction,
                self.skills_guidance,
                self.skills_catalog,
            ]
            if section
        )
```

- [ ] **Step 4: Update `Agent` to expose the separated parts**

```python
@property
def instruction_parts(self) -> SystemInstructionParts:
    return compose_system_instruction_parts(self.behavior_instruction, self.skills)
```

- [ ] **Step 5: Run the skills tests**

Run: `python3 -m pytest tests/agents/test_skills.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/myopenclaw/agents/skills.py src/myopenclaw/agents/agent.py tests/agents/test_skills.py
git commit -m "feat: split agent instruction parts for context usage"
```

### Task 3: Add Provider Token Estimation Hooks

**Files:**
- Modify: `src/myopenclaw/providers/base.py`
- Modify: `src/myopenclaw/providers/gemini.py`
- Modify: `tests/providers/test_gemini.py`

- [ ] **Step 1: Write the failing Gemini provider tests for token estimation**

```python
def test_estimate_text_tokens_uses_count_tokens_request_shape(self) -> None:
    provider = GeminiProvider(model="gemini-3-flash-preview")
    ...

def test_estimate_tool_tokens_builds_function_declarations(self) -> None:
    provider = GeminiProvider(model="gemini-3-flash-preview")
    ...
```

- [ ] **Step 2: Run the provider tests to verify they fail**

Run: `python3 -m pytest tests/providers/test_gemini.py -k estimate -v`
Expected: FAIL because the provider exposes no estimation API.

- [ ] **Step 3: Add provider estimation methods**

```python
class BaseLLMProvider(ABC):
    async def estimate_text_tokens(self, *, system_instruction: str | None = None) -> int | None:
        return None

    async def estimate_tools_tokens(self, tool_specs: list[ToolSpec]) -> int | None:
        return None
```

- [ ] **Step 4: Implement Gemini estimation using official token counting**

```python
async def estimate_text_tokens(self, *, system_instruction: str | None = None) -> int | None:
    response = await self.client.aio.models.count_tokens(
        model=self.model,
        config=types.CountTokensConfig(
            generate_content_request=types.GenerateContentRequest(
                system_instruction=system_instruction,
            ),
        ),
    )
    return getattr(response, "total_tokens", None)
```

```python
async def estimate_tools_tokens(self, tool_specs: list[ToolSpec]) -> int | None:
    response = await self.client.aio.models.count_tokens(
        model=self.model,
        config=types.CountTokensConfig(
            generate_content_request=types.GenerateContentRequest(
                tools=self._build_tools(tool_specs),
            ),
        ),
    )
    return getattr(response, "total_tokens", None)
```

- [ ] **Step 5: Factor request-building helpers only if needed to keep generation and estimation in sync**

Run: `python3 -m pytest tests/providers/test_gemini.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/myopenclaw/providers/base.py src/myopenclaw/providers/gemini.py tests/providers/test_gemini.py
git commit -m "feat: add gemini token estimation hooks"
```

### Task 4: Build Context Usage Snapshot Computation

**Files:**
- Create: `src/myopenclaw/runs/context_usage.py`
- Create: `tests/runs/test_context_usage.py`

- [ ] **Step 1: Write the failing tests for context snapshot calculation**

```python
async def test_snapshot_uses_last_metadata_total_and_residual_messages() -> None:
    snapshot = await service.build(agent=agent, context=context, session=session)
    assert snapshot.total_tokens == 7000
    assert snapshot.messages_tokens == 7000 - 3200 - 900 - 600
```

```python
async def test_snapshot_computes_free_space_from_model_limit() -> None:
    assert snapshot.free_tokens == 1048576 - 7000
```

```python
async def test_snapshot_handles_missing_metadata() -> None:
    assert snapshot.total_tokens is None
    assert snapshot.categories["messages"].token_count is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/runs/test_context_usage.py -v`
Expected: FAIL because the service does not exist.

- [ ] **Step 3: Implement snapshot dataclasses and service**

```python
@dataclass(frozen=True)
class ContextUsageSnapshot:
    model_label: str
    max_input_tokens: int | None
    total_tokens: int | None
    categories: list[ContextUsageCategory]
    free_tokens: int | None
```

```python
messages_tokens = (
    total_tokens - system_tokens - skills_tokens - tools_tokens
    if None not in (total_tokens, system_tokens, skills_tokens, tools_tokens)
    else None
)
```

- [ ] **Step 4: Use the latest persisted metadata as the total source of truth**

```python
def _latest_usage_metadata(self, session: Session) -> MessageMetadata | None:
    for message in reversed(session.messages):
        if message.metadata and message.metadata.input_tokens is not None:
            return message.metadata
    return None
```

- [ ] **Step 5: Add skills drill-down details to the snapshot**

```python
details=[
    ContextUsageDetail(label=skill.name, token_count=token_count)
    for skill, token_count in ...
]
```

- [ ] **Step 6: Run the new tests**

Run: `python3 -m pytest tests/runs/test_context_usage.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/myopenclaw/runs/context_usage.py tests/runs/test_context_usage.py
git commit -m "feat: compute resident context usage snapshots"
```

### Task 5: Render the `/context` View

**Files:**
- Create: `src/myopenclaw/cli/context_renderer.py`
- Modify: `tests/cli/test_chat_loop.py`

- [ ] **Step 1: Write the failing CLI rendering test**

```python
async def test_context_command_renders_usage_summary(self) -> None:
    ...
    self.assertIn("Context Usage", rendered)
    self.assertIn("Estimated usage by category", rendered)
    self.assertIn("System prompt", rendered)
    self.assertIn("Skills", rendered)
    self.assertIn("Messages", rendered)
    self.assertIn("Free space", rendered)
```

- [ ] **Step 2: Run the CLI test to verify it fails**

Run: `python3 -m pytest tests/cli/test_chat_loop.py -k context -v`
Expected: FAIL because `/context` is unsupported.

- [ ] **Step 3: Implement a dedicated Rich renderer**

```python
class ContextRenderer:
    def render(self, snapshot: ContextUsageSnapshot) -> RenderableType:
        return Group(
            Text("Context Usage", style="bold"),
            self._render_usage_header(snapshot),
            self._render_category_summary(snapshot),
            self._render_skills_breakdown(snapshot),
        )
```

- [ ] **Step 4: Use a stable visualization format that matches the Claude Code intent**

Run: keep the layout to:
- model label + `used/max`
- category totals
- skills breakdown section

Expected: no nested panels inside panels unless readability demands it.

- [ ] **Step 5: Run the CLI tests**

Run: `python3 -m pytest tests/cli/test_chat_loop.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/myopenclaw/cli/context_renderer.py tests/cli/test_chat_loop.py
git commit -m "feat: render context usage view"
```

### Task 6: Wire `/context` into the Chat Loop

**Files:**
- Modify: `src/myopenclaw/cli/chat.py`
- Modify: `src/myopenclaw/runs/__init__.py` if exports are needed
- Modify: `tests/cli/test_chat_loop.py`

- [ ] **Step 1: Write the failing command-routing test**

```python
async def test_help_lists_context_command(self) -> None:
    ...
    self.assertIn("/context", help_text)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/cli/test_chat_loop.py -k help -v`
Expected: FAIL because help text does not list `/context`.

- [ ] **Step 3: Update the command list and handler**

```python
if command == "/context":
    snapshot = await self._build_context_snapshot()
    self._render_context_snapshot(snapshot)
    return True
```

- [ ] **Step 4: Keep context computation out of `_handle_command` body**

```python
async def _render_context_command(self) -> None:
    snapshot = await self._context_usage_service.build(
        agent=self.agent,
        context=self.coordinator.context,
        session=self.session,
    )
```

- [ ] **Step 5: Run the focused CLI suite**

Run: `python3 -m pytest tests/cli/test_chat_loop.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/myopenclaw/cli/chat.py tests/cli/test_chat_loop.py
git commit -m "feat: add context command to chat loop"
```

### Task 7: Run End-to-End Verification

**Files:**
- Modify: none unless issues are found

- [ ] **Step 1: Run the focused test suites**

Run: `python3 -m pytest tests/config/test_app_config.py tests/providers/test_gemini.py tests/runs/test_context_usage.py tests/cli/test_chat_loop.py -v`
Expected: PASS

- [ ] **Step 2: Run the broader regression suite for touched areas**

Run: `python3 -m pytest tests/agents/test_skills.py tests/app/test_assembly.py -v`
Expected: PASS

- [ ] **Step 3: Smoke-test the command manually**

Run: `python3 -m myopenclaw.cli.main chat --config config.yaml`
Then type: `/context`
Expected:
- the command is accepted
- a context usage summary is printed
- the category list includes `System prompt`, `Skills`, `Messages`, `Tools`, and `Free space`

- [ ] **Step 4: Commit final verification fixes if needed**

```bash
git add <any-fixed-files>
git commit -m "test: verify context command behavior"
```

## Notes

- `Messages` in v1 is intentionally a residual category:
  `messages = total - system - skills - tools`
  Do not over-engineer per-message contributor accounting in this iteration.

- `Total context usage` in v1 comes from the latest persisted model-step metadata:
  `input_tokens + output_tokens`
  This aligns with the requirement to show the current resident context before the next user message.

- `Skills` should be shown as a separate category even though they are currently appended into `system_instruction`.

- `Tools` should represent the model-visible function declarations, not tool execution transcripts.

- If token estimation fails for one category, render `unknown` for that category instead of silently substituting a character-count heuristic.
