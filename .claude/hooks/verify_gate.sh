#!/usr/bin/env bash
# TaskCompleted hook: exit 2 prevents the task from being marked completed.
set -uo pipefail
INPUT=$(cat)
SUBJECT=$(echo "$INPUT" | jq -r '.task.subject // .subject // empty')
TID=$(echo "$SUBJECT" | grep -oE '^T-[0-9]+' || true)
[ -z "$TID" ] && exit 0   # not a ticket task; allow
VERIFY=$(jq -r --arg id "$TID" '.tickets[] | select(.id==$id) | .verify' \
  "${CLAUDE_PROJECT_DIR}/docs/tickets.json")
[ -z "$VERIFY" ] || [ "$VERIFY" = "null" ] && exit 0
cd "$CLAUDE_PROJECT_DIR"
if bash -c "$VERIFY"; then
  mkdir -p .claude/evidence && date -u +%s > ".claude/evidence/${TID}.pass"
  exit 0
else
  echo "verify failed for ${TID}: ${VERIFY} — ticket cannot be closed. Revert to the ticket-start commit and retry." >&2
  exit 2
fi
