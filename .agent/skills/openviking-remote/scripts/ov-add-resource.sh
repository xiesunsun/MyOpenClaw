#!/usr/bin/env bash
set -euo pipefail

: "${OPENVIKING_BASE_URL:?missing OPENVIKING_BASE_URL}"
: "${OPENVIKING_ACCOUNT_ID:?missing OPENVIKING_ACCOUNT_ID}"
: "${OPENVIKING_USER_ID:?missing OPENVIKING_USER_ID}"
: "${OPENVIKING_AGENT_ID:?missing OPENVIKING_AGENT_ID}"
: "${OPENVIKING_USER_KEY:?missing OPENVIKING_USER_KEY}"

LOCAL_FILE="${1:?missing local file path}"
TARGET_PARENT="${2:-viking://resources/}"

if [[ ! -f "$LOCAL_FILE" ]]; then
  echo "file not found: $LOCAL_FILE" >&2
  exit 1
fi

TMP_JSON="$(mktemp)"
trap 'rm -f "$TMP_JSON"' EXIT

curl -sS \
  -H "Authorization: Bearer ${OPENVIKING_USER_KEY}" \
  -H "X-OpenViking-Account: ${OPENVIKING_ACCOUNT_ID}" \
  -H "X-OpenViking-User: ${OPENVIKING_USER_ID}" \
  -H "X-OpenViking-Agent: ${OPENVIKING_AGENT_ID}" \
  -F "file=@${LOCAL_FILE}" \
  -F "telemetry=true" \
  "${OPENVIKING_BASE_URL}/api/v1/resources/temp_upload" > "$TMP_JSON"

TEMP_FILE_ID="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["result"]["temp_file_id"])' "$TMP_JSON")"

BODY="$(python3 -c 'import json, sys; print(json.dumps({
  "temp_file_id": sys.argv[1],
  "parent": sys.argv[2],
  "wait": False,
  "strict": True,
  "directly_upload_media": True
}))' "$TEMP_FILE_ID" "$TARGET_PARENT")"

"$(dirname "$0")/ov-api.sh" POST "/api/v1/resources" "$BODY"
