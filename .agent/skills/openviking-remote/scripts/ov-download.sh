#!/usr/bin/env bash
set -euo pipefail

: "${OPENVIKING_BASE_URL:?missing OPENVIKING_BASE_URL}"
: "${OPENVIKING_ACCOUNT_ID:?missing OPENVIKING_ACCOUNT_ID}"
: "${OPENVIKING_USER_ID:?missing OPENVIKING_USER_ID}"
: "${OPENVIKING_AGENT_ID:?missing OPENVIKING_AGENT_ID}"
: "${OPENVIKING_USER_KEY:?missing OPENVIKING_USER_KEY}"

URI="${1:?missing uri}"
OUTPUT_PATH="${2:?missing output path}"
ENCODED_URI="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$URI")"

curl -sS \
  -H "Authorization: Bearer ${OPENVIKING_USER_KEY}" \
  -H "X-OpenViking-Account: ${OPENVIKING_ACCOUNT_ID}" \
  -H "X-OpenViking-User: ${OPENVIKING_USER_ID}" \
  -H "X-OpenViking-Agent: ${OPENVIKING_AGENT_ID}" \
  "${OPENVIKING_BASE_URL}/api/v1/content/download?uri=${ENCODED_URI}" \
  -o "${OUTPUT_PATH}"
