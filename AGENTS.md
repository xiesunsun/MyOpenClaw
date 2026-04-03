# AGENTS.md

## Layer Dependency Diagram

```text
shared -> config/agents/conversations/providers/tools -> runs -> app -> cli
```

## Verification

Run this layer dependency check after every code change:

```bash
./.venv/bin/python scripts/lint.py
```
