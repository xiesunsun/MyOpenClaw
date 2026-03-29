# ReAct Tools and Runtime Design

## Goal

Extend MyOpenClaw from a single-step chat runtime into a multi-step ReAct agent runtime with structured tools, provider-level function calling, and model-visible tool observations stored in session history.

## Design

### Agent and tools

- `Agent` remains the static capability holder for workspace, behavior, model, and tools.
- `AgentDefinition` stores `tool_ids`, not provider-specific schemas.
- `ToolRegistry` resolves configured tool ids into executable tool objects.
- Each tool exposes a normalized `ToolSpec` plus an async `execute(arguments, context)` method.

### Session and message model

- `Session` remains the persistent model-visible transcript.
- `SessionMessage` becomes structured enough to represent user messages, assistant messages, assistant tool calls, and tool results.
- Runtime and provider logs remain out of session history.

### Runtime

- `AgentRuntime` owns the ReAct loop.
- A turn appends the user message, calls the model with tool schemas, executes returned tool calls, appends tool results, and repeats until the model returns a final answer or the step limit is reached.
- Runtime also enforces session/agent ownership, step limits, and tool error handling.

### Provider abstraction

- Internal LLM types become provider-neutral IR:
  - `ToolSpec`
  - `ToolCall`
  - `GenerateRequest`
  - `GenerateResult`
  - `FinishReason`
- `GeminiProvider` translates this IR to Gemini function declarations and structured responses.
- Provider does not execute tools.

## Config

- `config.yaml` gains `agents.<id>.tools`, a list of tool ids.
- Tool schemas are derived from resolved tool objects, not handwritten in config.

## First implementation scope

- Add normalized tool abstractions and a built-in `echo` tool for config/runtime verification.
- Upgrade session messages and runtime loop for multi-step tool execution.
- Upgrade Gemini request/response mapping for tool declarations and tool calls.
- Add tests for tool registry, config resolution, runtime loop behavior, and Gemini conversion helpers where possible.
