#!/usr/bin/env bash
set -euo pipefail

QUERY="${1:?missing query}"
TARGET_URI="${2:-viking://resources/}"
LIMIT="${3:-8}"

BODY="$(python3 -c 'import json, sys; print(json.dumps({
  "query": sys.argv[1],
  "target_uri": sys.argv[2],
  "limit": int(sys.argv[3]),
}))' "$QUERY" "$TARGET_URI" "$LIMIT")"

"$(dirname "$0")/ov-api.sh" POST "/api/v1/search/find" "$BODY"
