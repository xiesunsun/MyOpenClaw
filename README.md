# MyOpenClaw

MyOpenClaw is a local-first, extensible agent runtime built for coding and workspace automation scenarios. It focuses on the core execution loop of an agent system: model invocation, tool calling, multi-turn session management, context construction, persistence, and controlled workspace operations.

The implementation is intentionally kept concrete and inspectable rather than being a prompt-only demo.

## What It Does

- Runs an interactive CLI agent loop for local coding and automation tasks.
- Supports a provider abstraction with a working Google Gemini integration.
- Exposes 10+ built-in tools for directory listing, search, file reads/writes, exact replace, and persistent shell execution.
- Persists sessions in SQLite and supports listing, resuming, and deleting prior sessions.
- Builds prompt context from recent conversation turns and reports context usage in the CLI.
- Supports optional OpenViking-backed session sync and session recall for longer-running workflows.
- Includes focused tests around app assembly, config loading, session storage, runtime behavior, and shell/tool flows.

## Why I Built It

I built MyOpenClaw to understand and implement the runtime concerns behind coding agents instead of stopping at API wrappers:

- How tools are registered and executed safely.
- How a multi-turn session is persisted and resumed.
- How prompt context is assembled and bounded.
- How shell state is preserved across turns.
- How a local runtime can later sync to a remote memory/session service.

## Current Architecture

The codebase is organized around a few narrow modules:

- `agents`: agent metadata, behavior loading, and skill discovery.
- `app`: the composition root that assembles config, runtime, sessions, tools, and integrations.
- `cli`: interactive chat loop, context rendering, session commands, and terminal UX.
- `config`: YAML-based application config with env var expansion.
- `context`: recent-turn windowing and optional session recall injection.
- `conversations`: message/session models and session service orchestration.
- `persistence`: SQLite-backed session repository.
- `providers`: model provider abstraction and Gemini implementation.
- `runs`: turn coordinator, runtime context, ReAct strategy, and usage accounting.
- `tools`: built-in file tools, persistent shell tools, policies, and registry.
- `integrations/openviking`: optional remote session sync and session recall adapters.

## Key Capabilities

### 1. Extensible agent runtime

The runtime resolves an agent from config, loads its behavior prompt, attaches a model configuration, and binds a selected tool set. The assembly layer keeps provider, tool, context, and persistence concerns separate so the runtime stays composable.

### 2. Multi-turn session persistence and resume

Each chat session is stored in SQLite under `.myopenclaw/sessions.db`. The CLI supports:

- starting a new session
- resuming a session by `--session-id`
- listing recent sessions
- deleting sessions

This makes longer coding workflows inspectable and recoverable instead of transient.

### 3. Context window management

Prompt construction is based on recent user turns plus their associated assistant/tool activity, rather than replaying the full session blindly. The CLI also exposes a `/context` command to inspect how much context is currently being sent.

### 4. Controlled workspace operations

MyOpenClaw includes built-in file and shell tools commonly needed by a coding agent:

- `list_directory`
- `glob_search`
- `grep_search`
- `read_file`
- `read_many_files`
- `replace`
- `write_file`
- `shell_exec`
- `shell_restart`
- `shell_close`
- `echo`

For file operations, the runtime supports workspace-bounded access control as well as a full-access mode when explicitly configured.

### 5. Persistent shell state

Shell execution is backed by a PTY-based persistent shell session. This allows the agent to preserve working directory and shell process state across multiple commands, which is important for real coding workflows.

### 6. Optional OpenViking integration

The current branch also adds optional OpenViking support for:

- syncing local session messages to a remote service
- committing pending session state based on time/turn thresholds
- recalling related prior context into the next prompt

These capabilities are configuration-driven and can be disabled for fully local usage.

## Repository Status

This project is under active development. The current implementation already covers the runtime backbone needed for a practical local agent CLI, while a few areas are still intentionally evolving:

- broader provider support beyond Gemini
- richer skill/runtime packaging
- more advanced memory and retrieval strategies
- additional ergonomics for agent configuration and tool composition

## Quick Start

### Requirements

- Python 3.12+
- a Gemini API key if you want to run the default provider

### Install

```bash
git clone git@github.com:xiesunsun/MyOpenClaw.git
cd MyOpenClaw
uv sync
```

### Configure

The project reads configuration from `config.yaml`. Sensitive values can be injected through environment variables.

Example:

```bash
export OPENVIKING_AGENT_ID="..."
export OPENVIKING_BASE_URL="..."
export OPENVIKING_ACCOUNT_ID="..."
export OPENVIKING_USER_ID="..."
export OPENVIKING_USER_KEY="..."
```

For Gemini credentials, either:

- use the environment variable convention supported by `google-genai`
- or set `api_key` on the selected provider model config in `config.yaml`

The default config already shows the expected structure for:

- default model selection
- provider/model settings
- agent workspace and behavior prompt
- tool allowlist
- file access mode
- OpenViking toggles

### Run

Start a chat session:

```bash
uv run myopenclaw chat --config config.yaml
```

Run with a specific agent:

```bash
uv run myopenclaw chat --config config.yaml --agent Pickle
```

Resume a prior session:

```bash
uv run myopenclaw chat --config config.yaml --session-id <session-id>
```

List recent sessions:

```bash
uv run myopenclaw sessions --config config.yaml
```

Delete a session:

```bash
uv run myopenclaw sessions delete <session-id> --config config.yaml
```

## CLI Commands

Inside the interactive chat loop:

- `/help` shows available commands
- `/context` shows current context usage
- `/session` shows current session summary
- `/clear` redraws the screen
- `/exit` closes the session cleanly

## Testing

The repository contains test coverage for the runtime backbone, including:

- app assembly
- config/env expansion
- chat loop and session CLI commands
- session lifecycle and storage mapping
- SQLite persistence
- runtime event flow and runner behavior
- shell state handling
- OpenViking integration paths

Run tests with:

```bash
uv run pytest
```

## Roadmap

- add more provider backends behind the existing abstraction
- improve memory/retrieval quality beyond recent-turn windows
- expand reusable skills and agent packaging
- harden release/CI workflows for public usage

## License

Licensed under Apache-2.0. See `LICENSE`.
