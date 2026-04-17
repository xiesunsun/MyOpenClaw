---
name: openviking-remote
description: Use when the agent needs to browse, search, read, upload, download, or update context in a remote OpenViking service over HTTP using configured OPENVIKING_* environment variables, especially for viking:// resources, memories, sessions, semantic find, file reads, file uploads, session commit, or direct content/stat operations.
---

# OpenViking Remote

## Overview

Use the remote OpenViking HTTP API through small shell wrappers instead of re-building curl commands each time. Prefer user-key operations by default and switch to root-key calls only for privileged admin or recovery work.

## Environment

Expect these variables to already be exported:

- `OPENVIKING_BASE_URL`
- `OPENVIKING_ACCOUNT_ID`
- `OPENVIKING_USER_ID`
- `OPENVIKING_AGENT_ID`
- `OPENVIKING_USER_KEY`
- `OPENVIKING_ROOT_KEY`

Default auth behavior:

- normal operations use `OPENVIKING_USER_KEY`
- privileged operations use `--root` to switch to `OPENVIKING_ROOT_KEY`

## Workflow

1. Start with health or listing if remote availability is unclear.
2. Use `ov-find.sh` before broad reads when the user is asking for relevant context.
3. Use `ov-read.sh`, `ov-stat.sh`, or `ov-download.sh` for direct inspection.
4. Use `ov-write.sh` or `ov-add-resource.sh` only when the user explicitly wants remote changes.
5. Use `ov-session-commit.sh` only when the workflow really needs session persistence.

## Commands

Health:

```bash
scripts/ov-health.sh
```

List a directory:

```bash
scripts/ov-ls.sh "viking://resources/"
```

Read a text file:

```bash
scripts/ov-read.sh "viking://resources/path/file.md"
```

Get stat metadata:

```bash
scripts/ov-stat.sh "viking://resources/path/file.md"
```

Semantic search:

```bash
scripts/ov-find.sh "search words" "viking://resources/"
```

Download a file:

```bash
scripts/ov-download.sh "viking://resources/path/file.pdf" "/tmp/file.pdf"
```

Write text content:

```bash
scripts/ov-write.sh "viking://resources/path/file.md" "/absolute/local/text-file.md"
```

Upload a local file as a resource:

```bash
scripts/ov-add-resource.sh "/absolute/path/to/file.pdf" "viking://resources/"
```

Commit a session:

```bash
scripts/ov-session-commit.sh "session-id"
```

## Safety

- Prefer read operations first.
- Keep destructive or privileged operations behind explicit user intent.
- Use `--root` only for operations that genuinely need root privileges.
- Do not delete or move remote content unless the user explicitly asks.
