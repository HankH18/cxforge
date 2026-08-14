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
# be non-empty, and its LAST non-blank line (see T-13 below) must name a
# ticket id present in docs/tickets.json. Any failure of that chain is a
# DENY of every Edit/Write (other than the two unconditional protocol paths
# below). There is no env var, flag, sentinel value or magic path that
# substitutes for a real claim — unclaimed or unrecognised work is
# authorised only by a human editing .claude/active-ticket.
#
# T-13 (session-scoped, append-only ticket claims) changed .claude/active-
# ticket from a single mutable line to an APPEND-ONLY log (see
# .claude/hooks/claim_lookup.py for the exact line format and the reasoning
# below) — but left THIS hook deliberately session-BLIND. It reads
# claim_lookup.py's `--mode last` (the log's last non-blank line, whoever
# wrote it), never `--mode owned`. Do not "improve" this into a
# session-scoped check. Reasons, from T-13's own recon:
#   1. Different failure mode. The T-8/T-9/T-11 bug this hook's sibling
#      guards (stop_guard.sh, verify_gate.sh) were fixed for is an
#      OWNERSHIP/completion bug — a session told to "finish or revert"
#      work it never claimed. This hook is a PATH/SCOPE check ("is this
#      edit inside the claimed ticket's declared files") — it has never
#      told anyone to finish or revert anything, so it isn't that bug.
#   2. Blast radius. This hook fires on every single Edit/Write, live, in
#      whatever session currently holds the floor — including the session
#      implementing this very change. Subagents share their parent
#      session's CLAUDE_CODE_SESSION_ID (confirmed empirically during T-13
#      recon), so naive session-scoping would not even break subagent
#      fan-out — but a bug in a hook this hot still bricks the live build
#      immediately, versus a bug in Stop/TaskCompleted-only guards which is
#      contained to two much rarer transition points.
#   3. Ticket's own stated non-goal: "No multi-agent scheduling or lock
#      arbitration — this makes claims legible, it does not coordinate
#      them." Session-scoping this hook's allow/deny would turn a coarse
#      legibility check into a per-edit ownership/coordination gate, which
#      T-13 explicitly disclaims.
#
# UNCONDITIONAL PROTOCOL PATHS (acceptance 4 + 7), checked before any
# per-ticket scope lookup, and before the claim-record is even consulted:
#   .claude/active-ticket      -> ALLOW iff the call can only APPEND (see
#                                  APPEND-ONLY ENFORCEMENT below); otherwise
#                                  DENY.
#   .claude/evidence/**        -> DENY always (only verify_gate.sh may write
#                                  completion evidence; no ticket, including
#                                  one whose scope is .claude/hooks/**, may
#                                  write its own or another ticket's proof)
# Everything else under .claude/** and docs/** now falls through to normal
# per-ticket scope matching — there is no blanket allow for either tree.
#
# APPEND-ONLY ENFORCEMENT (T-13 acceptance 3; adversarial findings #2/#3):
# a plain Edit/Write to .claude/active-ticket used to be allowed
# unconditionally — including a full-file OVERWRITE, exactly what CLAUDE.md's
# (stale, pre-T-13) "write its ticket ID as the only line" instruction
# literally describes, which silently destroys the append-only audit trail
# T-13 exists to restore. This hook now asks claim_lookup.py's
# --mode append-check (fed the SAME PreToolUse payload this script already
# has, via stdin) whether the proposed Write's `content` — or the proposed
# Edit's old_string/new_string, simulated against the file's CURRENT on-disk
# content — would leave that current content as an exact prefix of the
# result. Only a call that can merely APPEND new lines (or the file doesn't
# exist yet — the very first claim) is allowed; anything that would alter or
# delete existing bytes is denied. The correct way to record a claim is
# .claude/hooks/claim.sh <ticket-id>, run via the Bash tool (see that
# script's header for why routing through Bash, not Edit/Write, is exactly
# the point — it bypasses this hook's Edit|Write-only matcher entirely,
# documented in the COVERAGE LIMITATION note above, and always appends).
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

# claim_lookup.py lives next to THIS script, not necessarily under
# CLAUDE_PROJECT_DIR — tests point CLAUDE_PROJECT_DIR at a synthetic,
# disposable project dir that only seeds docs/tickets.json and
# .claude/active-ticket, so resolve relative to this script's own location.
HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

AT="${CLAUDE_PROJECT_DIR}/.claude/active-ticket"
TICKETS="${CLAUDE_PROJECT_DIR}/docs/tickets.json"

# --- unconditional protocol paths (acceptance 4 + 7) ----------------------
case "$REL" in
  ".claude/active-ticket")
    APPEND_OK=$(printf '%s' "$INPUT" | python3 "$HOOK_DIR/claim_lookup.py" "$AT" --mode append-check)
    if [ "$APPEND_OK" = "ok" ]; then
      exit 0
    fi
    deny "$REL is an append-only, git-tracked claim log (T-13 acceptance 3): this Edit/Write would alter or remove existing content instead of only appending new lines. Use 'bash .claude/hooks/claim.sh <ticket-id>' (via the Bash tool) to record a well-formed claim — see that script and .claude/hooks/claim_lookup.py for the format." ;;
  ".claude/evidence/"*)
    deny "$REL is completion evidence; only verify_gate.sh may write .claude/evidence/** after a real verify run — no ticket may write or forge its own (or another ticket's) evidence." ;;
esac

# --- fail-closed claim record (acceptance 3) -------------------------------
if [ ! -s "$AT" ]; then
  deny "no ticket is claimed: .claude/active-ticket is missing or empty. A human must claim a ticket (write its id to that file) before any Edit/Write is authorised — there is no bypass."
fi

TID=$(python3 "$HOOK_DIR/claim_lookup.py" "$AT" --mode last)
if [ -z "$TID" ]; then
  deny ".claude/active-ticket has no resolvable ticket claim on its last non-blank line (whitespace-only content, or the log's most recent entry is a release marker). A human must record a real ticket id before any Edit/Write is authorised — there is no bypass."
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
