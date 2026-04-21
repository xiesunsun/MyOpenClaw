# Anthropic Provider Design

Date: 2026-04-21

## Goal

Add a direct Anthropic provider backed by the official Python SDK and Anthropic Messages API.

The first version must match the current Gemini provider capability surface:

- text generation
- multi-turn history
- tool calling
- tool result continuation
- request token counting
- provider metadata and usage extraction

The design should follow Anthropic's native request and response semantics as closely as possible and should avoid inventing provider-agnostic abstractions that flatten Anthropic-specific behavior incorrectly.

## Non-Goals

- Do not add Bedrock, Vertex, or OpenAI-compatible Anthropic support.
- Do not use Anthropic SDK `tool_runner`.
- Do not add streaming in this phase.
- Do not add prompt caching, beta headers, or structured-output features in this phase.
- Do not redesign the runtime execution loop or provider abstraction.

## Current State

The current codebase has a single concrete LLM provider:

- `google/gemini` in [`src/myopenclaw/providers/gemini.py`](/Users/ssunxie/code/myopenclaw/src/myopenclaw/providers/gemini.py)

The runtime interacts only with the provider abstraction:

- [`BaseLLMProvider`](/Users/ssunxie/code/myopenclaw/src/myopenclaw/providers/base.py)
- [`ReActStrategy`](/Users/ssunxie/code/myopenclaw/src/myopenclaw/runs/strategy/react.py)

Gemini request construction and response parsing both live inside the Gemini provider. The runtime does not know Gemini wire types.

Gemini already carries provider-specific continuation state through the domain model in a narrow form:

- Gemini `thought_signature` is stored on `ToolCall`
- it is persisted with `ToolCallBatch`
- it is replayed by the Gemini provider on the next request

This existing pattern is the model for Anthropic continuation handling.

## Design Summary

Add a new `anthropic` provider implementation that uses:

- `AsyncAnthropic.messages.create(...)`
- `AsyncAnthropic.messages.count_tokens(...)`

Anthropic request/response mapping remains isolated inside the new provider.

To support Anthropic adaptive thinking with tool use, the domain message model gains one new narrow provider-specific field:

- `SessionMessage.provider_thinking_blocks`

This field stores only the original Anthropic assistant `thinking` blocks that must be sent back unchanged during tool-use continuation. It does not store the full raw provider response.

Gemini remains on the current `thought_signature` path and does not migrate to the new field in this phase.

## Key Decisions

### 1. Provider name

Use provider id:

```text
anthropic
```

### 2. Model id

Use Anthropic's native model id:

```text
claude-opus-4-7
```

### 3. Temperature handling

`temperature` becomes nullable at the shared config level.

Reason:

- Anthropic Claude Opus 4.7 rejects non-default sampling parameters such as `temperature`, `top_p`, and `top_k`.
- A nullable field lets each provider decide whether to send the parameter at all.

Gemini behavior must not regress:

- if Gemini config leaves `temperature` unset, Gemini provider still uses its current effective default of `1.0`

### 4. Thinking config

Remove top-level `thinking_level` from shared model config.

Replace it with a single provider-specific config entry:

```yaml
provider_options:
  thinking: <level>
```

This field is interpreted by each provider separately.

Supported values for the initial Anthropic design:

- `low`
- `medium`
- `high`
- `max`
- `xhigh`

Anthropic mapping:

- missing or `null` `thinking` means thinking disabled
- configured `thinking` means:

```python
thinking = {"type": "adaptive", "display": "summarized"}
output_config = {"effort": <level>}
```

Gemini mapping:

- Gemini provider reads `provider_options["thinking"]`
- the provider maps it to Gemini's native thinking config the same way the old `thinking_level` field worked

### 5. Continuation state for Anthropic thinking

Add to `SessionMessage`:

```python
provider_thinking_blocks: list[dict[str, Any]] | None = None
```

This field stores only Anthropic `thinking` blocks from the assistant response.

It does not store:

- assistant text blocks
- tool use blocks
- tool result blocks
- full raw response objects

Reason:

- text is already stored in `SessionMessage.content`
- tool calls are already stored in `tool_call_batch.calls`
- tool results are already stored in `tool_call_batch.results`
- only Anthropic thinking blocks are missing from the current domain model and are required for correct continuation with tools

## Config Design

### Shared config shape

The shared model config remains:

- `api_key`
- `api_base`
- `temperature`
- `max_input_tokens`
- `max_output_tokens`
- `provider_options`

But changes are:

- `temperature: float | None`
- remove top-level `thinking_level`

### Anthropic config example

```yaml
providers:
  anthropic:
    models:
      claude-opus-4-7:
        api_key: ${ANTHROPIC_API_KEY}
        api_base: https://api.anthropic.com
        max_input_tokens: 1000000
        max_output_tokens: 64000
        provider_options:
          timeout_seconds: 600
          max_retries: 2
          thinking: xhigh
```

Anthropic provider behavior:

- `provider_options.thinking = xhigh`
  maps to adaptive thinking plus `output_config.effort = "xhigh"`
- `thinking` omitted
  means do not send Anthropic thinking config

### Gemini config example

```yaml
providers:
  google/gemini:
    models:
      gemini-3-flash-preview:
        temperature: 1.0
        max_input_tokens: 1048576
        max_output_tokens: 65536
        provider_options:
          thinking: low
```

## Anthropic Provider Design

Create:

- [`src/myopenclaw/providers/anthropic.py`](/Users/ssunxie/code/myopenclaw/src/myopenclaw/providers/anthropic.py)

Suggested public surface:

- `__init__(...)`
- `from_config(...)`
- `generate(...)`
- `count_request_tokens(...)`

Suggested private helpers:

- `_build_messages(...)`
- `_build_tools(...)`
- `_build_assistant_message_blocks(...)`
- `_build_tool_result_blocks(...)`
- `_extract_text(...)`
- `_extract_tool_calls(...)`
- `_extract_thinking_blocks(...)`
- `_extract_usage(...)`
- `_build_client(...)`

### Client construction

Use official SDK:

```python
AsyncAnthropic(
    api_key=api_key,
    base_url=api_base,
    timeout=provider_options["timeout_seconds"],
    max_retries=provider_options["max_retries"],
)
```

Only pass configured values that exist.

### `generate(request)` behavior

Construct a native Anthropic Messages API payload with:

- `model`
- `max_tokens`
- `messages`
- optional `system`
- optional `tools`
- optional `thinking`
- optional `output_config`

Do not send `temperature` to `claude-opus-4-7`.

Return `GenerateResult` with:

- `text` from Anthropic text blocks
- `tool_calls` from Anthropic `tool_use` blocks
- `finish_reason = FinishReason.TOOL_CALLS` when tool calls exist
- `finish_reason = FinishReason.STOP` otherwise
- `provider_finish_reason = response.stop_reason`
- `provider_finish_message = None`
- `provider_response_id = response.id`
- `provider_model_version = response.model`
- `usage` from `response.usage`
- `provider_thinking_blocks` populated with extracted assistant thinking blocks

### `_build_messages(...)`

Map domain messages to Anthropic messages.

#### User messages

```python
{
    "role": "user",
    "content": [{"type": "text", "text": message.content}],
}
```

#### Plain assistant messages

If no `provider_thinking_blocks` and no `tool_call_batch`:

```python
{
    "role": "assistant",
    "content": [{"type": "text", "text": message.content}],
}
```

#### Assistant messages with tool history

When `tool_call_batch` exists, rebuild the assistant message in native Anthropic block form:

1. prepend any `provider_thinking_blocks`
2. append assistant text block if `content` is non-empty
3. append one `tool_use` block per call

Then emit the follow-up user message containing `tool_result` blocks in call order.

#### Assistant messages with thinking continuity only

If `provider_thinking_blocks` exists and there is no `tool_call_batch`, prepend the thinking blocks before the assistant text block.

This keeps the provider responsible for its own continuation protocol.

### `_build_tools(...)`

Map current `ToolSpec` to Anthropic tools:

```python
{
    "name": tool.name,
    "description": tool.description,
    "input_schema": tool.input_schema,
}
```

Do not force Anthropic-specific tool abstractions into shared tool specs.

### `count_request_tokens(...)`

Use:

```python
await client.messages.count_tokens(...)
```

The payload should match the same `system`, `messages`, and `tools` shape as `generate(...)`.

If token counting fails, return `None`.

## Shared Data Model Changes

### `ModelConfig`

Modify [`src/myopenclaw/shared/model_config.py`](/Users/ssunxie/code/myopenclaw/src/myopenclaw/shared/model_config.py):

- change `temperature: float = 1.0` to `temperature: float | None = None`
- remove top-level `thinking_level`
- remove the validator logic that merges `thinking_level` into `provider_options`

The new design intentionally does not keep a compatibility shim. Config should be updated to the new schema directly.

### `SessionMessage`

Modify [`src/myopenclaw/conversations/message.py`](/Users/ssunxie/code/myopenclaw/src/myopenclaw/conversations/message.py):

- add `provider_thinking_blocks: list[dict[str, Any]] | None = None`

This field is provider-specific continuation state. It remains opaque to the runtime.

### `GenerateResult`

Modify [`src/myopenclaw/shared/generation.py`](/Users/ssunxie/code/myopenclaw/src/myopenclaw/shared/generation.py):

- add `provider_thinking_blocks: list[dict[str, Any]] | None = None`

This lets the provider return the exact blocks that should be stored on the resulting assistant message.

### Session persistence

Modify [`src/myopenclaw/conversations/session_storage_mapper.py`](/Users/ssunxie/code/myopenclaw/src/myopenclaw/conversations/session_storage_mapper.py):

- serialize `provider_thinking_blocks`
- deserialize `provider_thinking_blocks`

The existing base64 handling for Gemini `thought_signature` remains unchanged.

## Runtime Changes

### ReAct persistence path

Modify [`src/myopenclaw/runs/strategy/react.py`](/Users/ssunxie/code/myopenclaw/src/myopenclaw/runs/strategy/react.py):

- when appending an assistant tool batch, persist `result.provider_thinking_blocks` on the assistant `SessionMessage`
- when appending a final assistant message, persist `result.provider_thinking_blocks` on the assistant `SessionMessage`

The runtime still does not inspect provider thinking content.

### Session helpers

Modify [`src/myopenclaw/conversations/session.py`](/Users/ssunxie/code/myopenclaw/src/myopenclaw/conversations/session.py) as needed so assistant append helpers can accept `provider_thinking_blocks`.

## Factory and Wiring Changes

Modify:

- [`src/myopenclaw/providers/factory.py`](/Users/ssunxie/code/myopenclaw/src/myopenclaw/providers/factory.py)
- [`src/myopenclaw/providers/__init__.py`](/Users/ssunxie/code/myopenclaw/src/myopenclaw/providers/__init__.py)

Add:

- Anthropic provider import
- provider selection branch for `anthropic`

## Dependency Changes

Modify [`pyproject.toml`](/Users/ssunxie/code/myopenclaw/pyproject.toml):

- add `anthropic`

## Layering and Coupling Impact

### What does not change

- `runs` still depends only on `BaseLLMProvider`
- `conversations` still stores provider-agnostic core content
- provider wire-format mapping still lives inside each provider
- the execution loop remains owned by `ReActStrategy`

### What changes

- one new provider implementation is added
- one new narrow provider continuation field is added to `SessionMessage`
- shared config removes a Gemini-specific top-level field that was not truly cross-provider

### Coupling assessment

This change does not introduce a new architectural coupling direction.

The only intentional provider-specific domain leak is:

- `SessionMessage.provider_thinking_blocks`

This is acceptable because:

- it is minimal
- it is opaque to the runtime
- it mirrors the existing Gemini pattern where provider-specific continuation data already exists on `ToolCall.thought_signature`

## Why Anthropic SDK `tool_runner` Is Not Used

Anthropic SDK `tool_runner` manages its own tool loop, including:

- issuing requests
- handling tool use
- executing tool callbacks
- continuing the conversation automatically

This conflicts with the current architecture, where the runtime owns:

- tool execution
- session persistence
- event emission
- step counting
- tool result ordering

Therefore the Anthropic provider must remain a thin protocol adapter and must not take over the agent loop.

## Implementation Steps

1. Add `anthropic` dependency to `pyproject.toml`.
2. Update shared model config:
   - nullable `temperature`
   - remove top-level `thinking_level`
3. Update `config.yaml` and config-related tests to use `provider_options.thinking`.
4. Add `provider_thinking_blocks` to:
   - `SessionMessage`
   - `GenerateResult`
5. Update session persistence to round-trip `provider_thinking_blocks`.
6. Update session append helpers and ReAct strategy to persist provider thinking blocks.
7. Implement `AnthropicProvider`.
8. Update provider factory and exports.
9. Update README to document Anthropic support and config examples.
10. Run provider, config, runtime, and persistence tests.

## Test Plan

### New provider tests

Create:

- [`tests/providers/test_anthropic.py`](/Users/ssunxie/code/myopenclaw/tests/providers/test_anthropic.py)

Cover:

- tool schema mapping to Anthropic `tools`
- request message reconstruction from:
  - user messages
  - assistant text messages
  - assistant tool batches
  - assistant `provider_thinking_blocks`
- text extraction from Anthropic responses
- tool call extraction from `tool_use` blocks
- thinking block extraction from response content
- token usage extraction
- `count_tokens` payload shape
- token count failure fallback to `None`
- `provider_options.thinking` mapping to:
  - `thinking={"type":"adaptive","display":"summarized"}`
  - `output_config={"effort": level}`
- no `temperature` sent for `claude-opus-4-7`

### Persistence tests

Update:

- [`tests/conversations/test_session_storage_mapper.py`](/Users/ssunxie/code/myopenclaw/tests/conversations/test_session_storage_mapper.py)

Cover:

- `provider_thinking_blocks` round-trips through storage
- existing Gemini `thought_signature` still round-trips unchanged

### Config tests

Update:

- [`tests/config/test_app_config.py`](/Users/ssunxie/code/myopenclaw/tests/config/test_app_config.py)

Cover:

- Anthropic model config resolves correctly
- Gemini config resolves correctly with `provider_options.thinking`
- removed top-level `thinking_level` no longer appears in fixtures

### Runtime tests

Update:

- [`tests/runs/test_runner.py`](/Users/ssunxie/code/myopenclaw/tests/runs/test_runner.py)

Cover:

- `GenerateResult.provider_thinking_blocks` is persisted to assistant messages
- tool-loop continuation preserves provider thinking blocks

### Regression checks

Run at minimum:

- provider tests
- config tests
- session storage tests
- runner tests
- context usage tests for Gemini

The Gemini regression goal is:

- unchanged request construction except for reading `provider_options.thinking`
- unchanged `thought_signature` persistence and replay
- unchanged effective default temperature behavior

## Risks

### 1. Anthropic thinking + tools continuity

If `provider_thinking_blocks` are not persisted and replayed exactly, Anthropic adaptive thinking with tools will break or degrade.

### 2. Gemini config regression

Removing `thinking_level` and making `temperature` nullable can regress Gemini if:

- config fixtures are not updated
- Gemini provider does not keep its effective default temperature behavior

### 3. Over-storing provider payload

Storing full raw Anthropic responses would add unnecessary duplication and couple persistence too tightly to provider internals. This design intentionally stores only the minimal missing continuation data.

## Acceptance Criteria

The work is complete when all of the following are true:

- `anthropic` provider can be selected from `config.yaml`
- `claude-opus-4-7` requests use official Anthropic SDK and native request fields
- `provider_options.thinking` drives Anthropic adaptive thinking effort
- Anthropic tool-use turns replay required thinking blocks correctly
- Gemini still works with the updated config model
- session persistence round-trips both:
  - Gemini `thought_signature`
  - Anthropic `provider_thinking_blocks`
- runtime behavior and provider selection remain layered as before
