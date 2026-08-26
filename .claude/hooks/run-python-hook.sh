#!/usr/bin/env bash
# Run a Claude Code Python hook with the interpreter available on the host.
set -eu

project_dir=${CLAUDE_PROJECT_DIR:-.}
script=${1:?hook script is required}
shift

if command -v python >/dev/null 2>&1; then
    exec python "$project_dir/$script" "$@"
fi
if command -v python3 >/dev/null 2>&1; then
    exec python3 "$project_dir/$script" "$@"
fi

printf 'Claude Code hook skipped: neither python nor python3 is available.\n' >&2
exit 127
