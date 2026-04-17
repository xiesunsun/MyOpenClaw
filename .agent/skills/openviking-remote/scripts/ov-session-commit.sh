#!/usr/bin/env bash
set -euo pipefail

SESSION_ID="${1:?missing session id}"

"$(dirname "$0")/ov-api.sh" POST "/api/v1/sessions/${SESSION_ID}/commit" "{}"
