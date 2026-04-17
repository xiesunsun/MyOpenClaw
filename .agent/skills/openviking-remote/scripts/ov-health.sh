#!/usr/bin/env bash
set -euo pipefail

: "${OPENVIKING_BASE_URL:?missing OPENVIKING_BASE_URL}"

echo "# /health"
curl -sS "${OPENVIKING_BASE_URL}/health"
echo
echo "# /api/v1/system/status"
"$(dirname "$0")/ov-api.sh" GET "/api/v1/system/status"
