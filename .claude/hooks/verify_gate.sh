#!/usr/bin/env bash
# Ticket completion gate.
#
# Wired to TWO events (see .claude/settings.json):
#   * TaskCompleted  — fires when a task is marked completed.
#   * PreToolUse[TaskUpdate] — belt and braces, and the one that is
#     verifiable from inside a session.
#
# Exit 2 blocks; stderr becomes the reason the model sees.
#
# HISTORY: the original version read the subject from `.task.subject`. The
# real TaskCompleted payload carries a TOP-LEVEL `task_subject`, so the jq
# expression returned empty, TID was empty, and the script took its
# "not a ticket task; allow" branch on every single call. The gate fired
# and rubber-stamped everything. Hence the defensive multi-shape parsing
# and the explicit no-silent-allow rule below.
set -uo pipefail
INPUT=$(cat)

TOOL=$(echo "$INPUT" | jq -r '.tool_name // empty')
if [ -n "$TOOL" ]; then
  # PreToolUse shape. Only gate the transition INTO completed.
  [ "$TOOL" = "TaskUpdate" ] || exit 0
  STATUS=$(echo "$INPUT" | jq -r '.tool_input.status // empty')
  [ "$STATUS" = "completed" ] || exit 0
fi

# Subject field name differs per event; try every documented shape.
SUBJECT=$(echo "$INPUT" | jq -r '
  .task_subject // .task.subject // .subject // .tool_input.subject // empty')
TID=$(echo "$SUBJECT" | grep -oE 'T-[0-9]+' | head -n1 || true)

# A pure status-change TaskUpdate carries no subject. Fall back to the
# claimed ticket, which the build protocol keeps current.
AT="${CLAUDE_PROJECT_DIR}/.claude/active-ticket"
if [ -z "$TID" ] && [ -f "$AT" ]; then
  TID=$(head -n1 "$AT" | tr -d '[:space:]')
fi

# No ticket identifiable: a non-ticket task, genuinely allowed.
[ -z "$TID" ] && exit 0

TICKETS="${CLAUDE_PROJECT_DIR}/docs/tickets.json"
VERIFY=$(jq -r --arg id "$TID" \
  '.tickets[] | select(.id==$id) | .verify' "$TICKETS" 2>/dev/null)

# An unknown ticket id, or a ticket with no verify command, must NOT be a
# silent pass — that is exactly the failure mode this script already had.
if [ -z "$VERIFY" ] || [ "$VERIFY" = "null" ]; then
  echo "verify_gate: no verify command found for ${TID} in docs/tickets.json." \
       "Refusing to close a ticket that cannot be verified." >&2
  exit 2
fi

cd "$CLAUDE_PROJECT_DIR" || exit 2
if bash -c "$VERIFY"; then
  mkdir -p .claude/evidence && date -u +%s > ".claude/evidence/${TID}.pass"
  exit 0
fi

rm -f ".claude/evidence/${TID}.pass"
echo "verify failed for ${TID}: ${VERIFY} — ticket cannot be closed." \
     "Revert to the ticket-start commit and retry." >&2
exit 2
