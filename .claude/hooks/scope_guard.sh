#!/usr/bin/env bash
# PreToolUse hook (Edit|Write): deny paths outside the active ticket's scope globs.
set -uo pipefail
INPUT=$(cat)
FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')
[ -z "$FILE" ] && exit 0
AT="${CLAUDE_PROJECT_DIR}/.claude/active-ticket"
[ -f "$AT" ] || exit 0            # no active ticket claimed; don't block setup work
TID=$(head -n1 "$AT" | tr -d '[:space:]')
REL="${FILE#"$CLAUDE_PROJECT_DIR"/}"
case "$REL" in .claude/*|docs/*) exit 0 ;; esac   # meta paths always allowed
MATCH=$(jq -r --arg id "$TID" --arg f "$REL" '
  .tickets[] | select(.id==$id) | .scope[] as $g
  | select(($f | test(($g | gsub("\\.";"\\.") | gsub("\\*\\*";".∞") | gsub("\\*";"[^/]*") | gsub("∞";"*")) + "$"))) | "yes"' \
  "${CLAUDE_PROJECT_DIR}/docs/tickets.json" | head -n1)
if [ "$MATCH" = "yes" ]; then exit 0; fi
cat <<EOF
{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"$REL is outside ticket $TID scope. If genuinely required, this is a plan defect — stop and ask the human."}}
EOF
exit 0
