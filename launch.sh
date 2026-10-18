#!/usr/bin/env bash
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
launcher="$project_dir/.venv/bin/scroll-capture"

if [[ ! -x "$launcher" ]]; then
    printf 'Scroll Capture is not installed in this project’s virtual environment.\n' >&2
    printf 'From %s, run:\n' "$project_dir" >&2
    printf '  python3 -m venv .venv\n  .venv/bin/python -m pip install -e .\n' >&2
    exit 1
fi

exec "$launcher" "$@"
