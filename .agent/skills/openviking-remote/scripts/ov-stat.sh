#!/usr/bin/env bash
set -euo pipefail

URI="${1:?missing uri}"
ENCODED_URI="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$URI")"

"$(dirname "$0")/ov-api.sh" GET "/api/v1/fs/stat?uri=${ENCODED_URI}"
