<p align="center">
  <img src="./.github/assets/pickel_agent_logo.png" alt="Pickel Agent Logo" width="360"/>
</p>

<h1 align="center">Pickel Agent</h1>

<p align="center">
  <strong>A local-first, highly extensible AI Agent runtime for coding and workspace automation</strong>
</p>

<p align="center">
  <a href="./README.md">中文</a> · <a href="./README.en.md">English</a>
</p>

---

**Pickel Agent** is a local-first, highly extensible Agent Runtime designed specifically for coding and workspace automation scenarios. It addresses the core runtime concerns of running AI coding assistants locally: **safe & sandbox-controlled tool execution, multi-turn session persistence, persistent high-fidelity shell sessions, fine-grained context window management, and optional cloud sync integration.**

Unlike prompt-only demos, Pickel Agent provides an industrial-grade local CLI engine and an extremely flexible, hot-pluggable Python Skills system. It allows developers to quickly build, load, and run custom developer tools while integrating seamlessly with state-of-the-art LLMs (e.g., Gemini, Claude).

---

## Key Technical Highlights

### 1. Context-Aware Persistent PTY Shell Sessions
Standard agents run shell commands in isolated, transient subprocesses, failing to preserve the working directory, environment variables, or execution state across commands (e.g., they cannot install dependencies in one step and run the dev server in the next command seamlessly). Pickel Agent features a custom PTY-backed persistent shell that keeps the execution state alive and continuous across turns.

### 2. Sandbox-Safe Controlled Tools
Security is a primary concern for local coding agents. Pickel Agent has a built-in workspace file service with fine-grained access control (Full/Sandbox levels). It comes with 10+ secure file, search, precise edit (exact-replace), and command execution tools to keep the agent bounded within the targeted workspace directory.

### 3. Dual-Layer Context Orchestration via /context & OpenViking
Managing token usage and history recall is critical for long-running coding sessions. Pickel Agent introduces an innovative dual-layer context management framework:
- **Local Micro-Observability (`/context`)**: Leverages smart sliding windows to slice prompt history. Inside the CLI, developers can execute the `/context` command to diagnose the precise token size, usage, and boundary window currently sent to the model.
- **Cloud Macro-Sync & Semantic Recall (`OpenViking`)**: Fully integrates with OpenViking sync protocols. It automatically commits session progress based on configurable time or turn thresholds, and performs semantic search to recall relevant historical session context, overcoming local memory limits.

### 4. Multi-Agent Isolation & Plug-and-Play Skills Ecosystem
To solve complex, multi-layered automation tasks, Pickel Agent natively establishes a robust multi-agent paradigm:
- **Strict Multi-Agent Isolation**: Configure distinct Agents (e.g., `Pickle`, `Architect`) in `config.yaml` with their own system prompts, workspace paths, custom tool registries, and custom model provider drivers. Boot any target sandbox instantly via `--agent AgentName`.
- **Hot-Pluggable Python Skills**: Effortlessly extend your agent's capabilities by writing Python scripts and Markdown rule definitions. Skills are auto-discovered, packaged, and dynamically registered as system tools on the fly.

---

## Core Visual Demonstrations

### 1. Interactive TUI Chat Loop & Agent Startup
Launch a beautiful, fluent TUI chat loop instantly based on configuration files and chosen agent behaviors.
![TUI Startup](./.github/assets/pickel_agent_start.png)

### 2. Transparent ReAct Reasoning Loop
Observe the agent's Thought -> Action -> Observation cycle clearly in real-time, giving you full control and transparency.
![ReAct Reasoning Pattern](./.github/assets/pickel_agent_react.png)

### 3. Precision Development Toolset
Safely scan directories, search codebase patterns, and write precise edits with high-accuracy exact-replace mechanisms.
![Built-in Tools](./.github/assets/pickel_agent_builtin_tools.png)

### 4. Context Diagnostics & Token Accounting
Run CLI command diagnostics like `/context` to trace prompt construction, sliding windows, and token counts.
![Context Diagnostics](./.github/assets/pickel_agent_session_context_command.png)

### 5. Modular Skill Customization
Dynamically inject customized instructions and toolsets to adapt the agent's behavior for specific repositories.
![Modular Skill](./.github/assets/pickel_agent_skill.png)

---

## Quick Start

### Requirements
- Python 3.12+
- Gemini or Anthropic API credentials (corresponding to your model driver choice)

### 1. Installation
```bash
# Clone the repository
git clone https://github.com/xiesunsun/Pickel-Agent.git
cd Pickel-Agent

# Sync dependencies using uv
uv sync
```

### 2. Configure Credentials
The project reads configurations from `config.yaml` in the root directory. Sensitive credentials can be injected via environment variables.
```bash
# Gemini credentials
export GEMINI_API_KEY="your-gemini-api-key"

# Anthropic credentials
export ANTHROPIC_API_KEY_PICKLE="your-anthropic-api-key"
```
You can adjust default model settings, security clearance (Sandbox/Full), and tool allowlists directly in `config.yaml`.

### 3. Run Interactive Sessions
```bash
# Default: layered config discovery (~/.pickel + project .pickel / agents)
uv run pickel chat
uv run pickel chat --agent Pickle
uv run pickel sessions
uv run pickel chat --session-id <session-id>
uv run pickel sessions delete <session-id>
# Legacy yaml: uv run pickel chat --config config.yaml
```

---

## TUI Slash Commands

Inside the interactive chat loop, you can use the following helper commands:

- `/help` - Show available slash commands list
- `/context` - Inspect active context token details and active sliding windows
- `/session` - Retrieve a detailed summary of the current session lifecycle
- `/clear` - Redraw/clear the terminal console screen
- `/exit` - Safely persist session state and exit cleanly

---

## Technical Stack

- **Language Core**: Python 3.12+ (driven by `uv` high-performance dependency manager)
- **LLM Drivers**: Google GenAI SDK (Gemini 3.0/3.1), Anthropic Python SDK (Claude/Jupiter)
- **TUI & Console**: Prompt-Toolkit, Typer, Rich
- **Sync Integration**: OpenViking (optional remote sync adapters)
- **Local Persistence**: SQLite Database (`~/.pickel/sessions.db`; override with `PICKEL_HOME`)

---

## Project Structure

```
src/
  pickel/               # Core execution package (for pickel CLI command)
    agents/                 # Agent behavior prompt loading and skill lookup
    app/                    # Application Composition Root and startup harness
    cli/                    # TUI rendering and terminal interaction layers
    config/                 # YAML configuration parser and env expansion utilities
    context/                # Prompt window sliding and token orchestration
    conversations/          # Conversation session models and persistence boundaries
    persistence/            # SQLite-backed persistence repository implementation
    providers/              # Gemini and Anthropic model driver implementations
    runs/                   # Session turn coordinators and ReAct reasoning strategies
    tools/                  # Workspace file utilities and PTY persistent terminal shell tools
    integrations/openviking # Sync services and context recall adapters for OpenViking
tests/                      # Suite covering assembly, persistence, PTY shell, and tool behaviors
```

---

## Testing

Run tests locally to verify that all components are fully functional:
```bash
uv run pytest
```

---

## Roadmap

- [ ] Support rich multimodal tools for local files (e.g., PDF extraction and image parsing)
- [ ] Incorporate local Vector Databases for deep, long-term memory retrieval
- [ ] Adapt more open-source local model backends (such as Ollama, llama.cpp)
- [ ] Develop a lightweight React-based local Web UI workspace panel

---

## License

This repository is licensed under the [Apache-2.0](./LICENSE) license.
