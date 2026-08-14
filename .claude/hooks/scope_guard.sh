#!/usr/bin/env bash
# PreToolUse hook: deny Edit/Write calls whose target path falls outside the
# currently-claimed ticket's declared scope globs in docs/tickets.json.
#
# CONTRACT (do not change): reads the PreToolUse payload as JSON on stdin;
# ALWAYS exits 0. A deny decision is communicated entirely through the JSON
# body on stdout (hookSpecificOutput.permissionDecision = "deny"), never
# through the process exit code — Claude Code treats a nonzero exit here as
# a hook execution ERROR, not as a deny. Silence on stdout + exit 0 means
# "allow".
#
# COVERAGE LIMITATION — READ BEFORE TRUSTING THIS AS A SANDBOX:
#   .claude/settings.json wires this hook to PreToolUse matcher "Edit|Write"
#   ONLY. Any write performed through another route — a Bash-tool shell
#   redirect (`echo x > file`), `sed -i`, `cp`, `git checkout <ref> -- path`,
#   a script that opens and writes a file itself — is INVISIBLE to this hook
#   and is completely unguarded. This script restricts two tools, not the
#   filesystem. Do not mistake it for an enforcement boundary against a
#   determined or buggy agent using Bash.
#
# FAIL-CLOSED CLAIM RULE (acceptance 3): .claude/active-ticket must exist,
# have a non-blank first line, and that line must name a ticket id present
# in docs/tickets.json. Any failure of that chain is a DENY of every
# Edit/Write (other than the two unconditional protocol paths below). There
# is no env var, flag, sentinel value or magic path that substitutes for a
# real claim — unclaimed or unrecognised work is authorised only by a human
# editing .claude/active-ticket.
#
# UNCONDITIONAL PROTOCOL PATHS (acceptance 4 + 7), checked before any
# per-ticket scope lookup, and before the claim-record is even consulted:
#   .claude/active-ticket      -> ALLOW always (this is how a claim is made)
#   .claude/evidence/**        -> DENY always (only verify_gate.sh may write
#                                  completion evidence; no ticket, including
#                                  one whose scope is .claude/hooks/**, may
#                                  write its own or another ticket's proof)
# Everything else under .claude/** and docs/** now falls through to normal
# per-ticket scope matching — there is no blanket allow for either tree.
#
# GLOB SEMANTICS for docs/tickets.json .scope entries:
#   - a match is a FULL match of the path relative to CLAUDE_PROJECT_DIR,
#     anchored at both the start and the end — a scope entry never matches
#     as a mid-path or suffix substring of an unrelated path
#   - "**" matches zero or more path segments and DOES cross "/"
#   - a single "*" matches within one path segment only and does NOT cross
#     "/"
#   - every other character, including ".", is matched LITERALLY
#   - a scope entry with no wildcard at all matches that exact relative path
#     and nothing else
#
# PATH RESOLUTION (acceptance 2): both CLAUDE_PROJECT_DIR and the target
# file are normalised with Python's os.path.realpath before any comparison
# — chosen over the `realpath`/`readlink -f` CLI because their flags differ
# between macOS/BSD and Linux/GNU, and because os.path.realpath correctly
# normalises a path that does not exist yet (a Write creates a new file) on
# both platforms. If the normalised target does not resolve under the
# normalised project dir, this hook exits 0 silently — out-of-repo scratch
# work, including anything that only *looks* in-scope before "../" is
# resolved, is never this guard's business.
set -uo pipefail

INPUT=$(cat)
FILE=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty')
[ -z "$FILE" ] && exit 0

deny() {
  local reason="$1"
  local reason_json
  reason_json=$(printf '%s' "$reason" | jq -Rs .)
  printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":%s}}\n' \
    "$reason_json"
  exit 0
}

# --- realpath resolution (acceptance 2), BEFORE any claim/scope lookup ---
REAL_PROJECT=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$CLAUDE_PROJECT_DIR")
REAL_FILE=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$FILE")

case "$REAL_FILE" in
  "$REAL_PROJECT"/*) : ;;   # inside the project — keep going
  *) exit 0 ;;               # not under CLAUDE_PROJECT_DIR at all: not our business
esac
REL="${REAL_FILE#"$REAL_PROJECT"/}"

# --- unconditional protocol paths (acceptance 4 + 7) ----------------------
case "$REL" in
  ".claude/active-ticket")
    exit 0 ;;
  ".claude/evidence/"*)
    deny "$REL is completion evidence; only verify_gate.sh may write .claude/evidence/** after a real verify run — no ticket may write or forge its own (or another ticket's) evidence." ;;
esac

# --- fail-closed claim record (acceptance 3) -------------------------------
AT="${CLAUDE_PROJECT_DIR}/.claude/active-ticket"
TICKETS="${CLAUDE_PROJECT_DIR}/docs/tickets.json"

if [ ! -s "$AT" ]; then
  deny "no ticket is claimed: .claude/active-ticket is missing or empty. A human must claim a ticket (write its id to that file) before any Edit/Write is authorised — there is no bypass."
fi

TID=$(head -n1 "$AT" | tr -d '[:space:]')
if [ -z "$TID" ]; then
  deny ".claude/active-ticket is whitespace-only. A human must record a real ticket id before any Edit/Write is authorised — there is no bypass."
fi

KNOWN=$(jq -r --arg id "$TID" '.tickets[] | select(.id==$id) | .id' "$TICKETS" 2>/dev/null | head -n1)
if [ "$KNOWN" != "$TID" ]; then
  deny "active ticket '$TID' (from .claude/active-ticket) is not a known ticket id in docs/tickets.json. Cannot authorise any scope for an unrecognised claim."
fi

# --- per-ticket scope match (acceptance 1) ---------------------------------
MATCH=$(jq -r --arg id "$TID" '.tickets[] | select(.id==$id) | .scope[]' "$TICKETS" | \
  REL="$REL" python3 -c '
import os
import re
import sys


def glob_to_regex(glob: str) -> "re.Pattern[str]":
    out = []
    i = 0
    n = len(glob)
    while i < n:
        if glob[i:i + 2] == "**":
            out.append(".*")
            i += 2
            continue
        c = glob[i]
        if c == "*":
            out.append("[^/]*")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^(?:" + "".join(out) + ")$")


rel = os.environ["REL"]
for raw in sys.stdin:
    pattern = raw.rstrip("\n")
    if not pattern:
        continue
    if glob_to_regex(pattern).match(rel):
        print("yes")
        break
')

if [ "$MATCH" = "yes" ]; then
  exit 0
fi

deny "$REL is outside ticket $TID's scope (docs/tickets.json). If genuinely required, this is a plan defect — stop and ask the human."
