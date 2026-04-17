#!/usr/bin/env bash
set -euo pipefail

URI="${1:?missing uri}"
LOCAL_TEXT_FILE="${2:?missing local text file}"
MODE="${3:-replace}"

if [[ ! -f "$LOCAL_TEXT_FILE" ]]; then
  echo "file not found: $LOCAL_TEXT_FILE" >&2
  exit 1
fi

BODY="$(python3 -c 'import json, pathlib, sys; print(json.dumps({
  "uri": sys.argv[1],
  "content": pathlib.Path(sys.argv[2]).read_text(),
  "mode": sys.argv[3],
  "wait": True
}))' "$URI" "$LOCAL_TEXT_FILE" "$MODE")"

"$(dirname "$0")/ov-api.sh" POST "/api/v1/content/write" "$BODY"
