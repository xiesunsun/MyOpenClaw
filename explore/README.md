# OpenViking Long-Term Memory Exploration

This folder contains small, non-production scripts for inspecting how myopenclaw can read OpenViking long-term memory.

## Environment

The scripts use the same environment variables as the main app:

- `OPENVIKING_BASE_URL`
- `OPENVIKING_USER_KEY`
- `OPENVIKING_ACCOUNT_ID`
- `OPENVIKING_USER_ID`
- `OPENVIKING_AGENT_ID`

The API key is used for authentication but is never printed.

## Run

```bash
uv run python explore/openviking_long_term_memory.py
```

Optional:

```bash
uv run python explore/openviking_long_term_memory.py --query "User's coding preferences" --limit 5
```

