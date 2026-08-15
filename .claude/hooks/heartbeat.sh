#!/usr/bin/env bash
# Delegates to harness_lib.py's hook-heartbeat (PostToolUse monitor pulse; appends
# .claude/monitor/heartbeat.jsonl). Protocol (T-22): ticket status is DERIVED, never
# stored, so no ticket boundary needs an agent-side Edit/Write of docs/tickets.json or
# docs/TASKS.md -- cmd_close regenerates TASKS.md itself.
exec python3 "${CLAUDE_PROJECT_DIR}/.claude/scripts/harness_lib.py" hook-heartbeat
