#!/usr/bin/env bash
set -euo pipefail

URI="${1:-viking://resources/}"
ENCODED_URI="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$URI")"

"$(dirname "$0")/ov-api.sh" GET "/api/v1/fs/ls?uri=${ENCODED_URI}&show_all_hidden=true"
