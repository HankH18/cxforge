#!/usr/bin/env bash
# Thin wrapper over the python harness (no jq dependency; python3 + git only).
exec python3 "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}/.claude/scripts/harness_lib.py" \
  "${1:-status_board}" "${@:2}"
