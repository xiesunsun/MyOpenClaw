# OpenViking SDK Smoke Test

This example uses the official OpenViking Python SDK against the remote server.

## Environment

The script reads these environment variables:

- `OPENVIKING_BASE_URL`
- `OPENVIKING_USER_KEY`
- `OPENVIKING_AGENT_ID`

Optional:

- `OPENVIKING_SDK_TEST_ROOT`

Default test root:

- `viking://resources/sdk-smoke`

## Run

```bash
source ~/.zshrc
uv run --with openviking python examples/openviking_sdk/smoke_test.py
```

## What It Does

The script performs a small end-to-end check:

1. Connects with `SyncHTTPClient`
2. Calls `initialize()`
3. Creates a test directory with `mkdir()`
4. Lists and stats that directory with `ls()` and `stat()`
5. Runs `find()` against the indexed OpenViking README
6. Creates a session and adds user/assistant messages
7. Runs `search()` both with and without session context
8. Commits the session

If session-aware search is still broken on the server, the script prints the failure instead of crashing silently.
