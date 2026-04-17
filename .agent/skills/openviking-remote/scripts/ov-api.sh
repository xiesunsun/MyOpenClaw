#!/usr/bin/env bash
set -euo pipefail

: "${OPENVIKING_BASE_URL:?missing OPENVIKING_BASE_URL}"
: "${OPENVIKING_ACCOUNT_ID:?missing OPENVIKING_ACCOUNT_ID}"
: "${OPENVIKING_USER_ID:?missing OPENVIKING_USER_ID}"
: "${OPENVIKING_AGENT_ID:?missing OPENVIKING_AGENT_ID}"

KEY="${OPENVIKING_USER_KEY:-}"
if [[ "${1:-}" == "--root" ]]; then
  shift
  KEY="${OPENVIKING_ROOT_KEY:-}"
fi

: "${KEY:?missing OPENVIKING_USER_KEY or OPENVIKING_ROOT_KEY}"

METHOD="${1:?missing method}"
PATH_PART="${2:?missing path}"
BODY="${3:-}"

COMMON_HEADERS=(
  -H "Authorization: Bearer ${KEY}"
  -H "X-OpenViking-Account: ${OPENVIKING_ACCOUNT_ID}"
  -H "X-OpenViking-User: ${OPENVIKING_USER_ID}"
  -H "X-OpenViking-Agent: ${OPENVIKING_AGENT_ID}"
)

if [[ -n "$BODY" ]]; then
  curl -sS -X "$METHOD" \
    "${COMMON_HEADERS[@]}" \
    -H "Content-Type: application/json" \
    "${OPENVIKING_BASE_URL}${PATH_PART}" \
    -d "$BODY"
else
  curl -sS -X "$METHOD" \
    "${COMMON_HEADERS[@]}" \
    "${OPENVIKING_BASE_URL}${PATH_PART}"
fi
