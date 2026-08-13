#!/usr/bin/env bash
set -uo pipefail
INPUT=$(cat)
ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false')
[ "$ACTIVE" = "true" ] && exit 0        # REQUIRED guard: prevents infinite loop
AT="${CLAUDE_PROJECT_DIR}/.claude/active-ticket"
[ -f "$AT" ] || exit 0
TID=$(head -n1 "$AT" | tr -d '[:space:]')
[ -f "${CLAUDE_PROJECT_DIR}/.claude/evidence/${TID}.pass" ] && exit 0
echo "{\"decision\":\"block\",\"reason\":\"Ticket ${TID} is claimed but has no passing verification. Finish it (verify must pass), revert and retry, or release the claim and explain why before stopping.\"}"
exit 0
