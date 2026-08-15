# Build Execution Protocol (cc-factory harness)

Approved plan: docs/SPEC.md (intent), docs/DESIGN.md (contracts),
docs/tickets.json (task graph — AUTHORITATIVE and read-only).
docs/TASKS.md is generated; never hand-edit it. Ticket status is never
stored anywhere — it is derived: receipt in .claude/evidence/ = resolved,
claim in .claude/claims/ = in progress, otherwise queued.
Task list name: cxforge (a visibility mirror only; the claim ledger rules).

## Rules

1. **No plan mode, no re-planning, no redesigning.** If the plan is wrong,
   record the defect in .claude/NEEDS_HUMAN.md and stop. Never edit
   docs/tickets.json, docs/SPEC.md, docs/DESIGN.md, or anything under
   .claude/hooks|scripts — not with Edit, not with Bash. The close-time
   integrity check fails the ticket if you do.
2. **All ticket lifecycle goes through `.claude/scripts/claim.sh`** —
   claim / close / release / status. Nothing else. Claiming requires a
   one-line ordering note (why this ticket next); if docs/TASKS.md or the
   plan defines a priority order, follow it and say so in the note.
3. **One ticket at a time.** `claim.sh claim T-x "note"` makes the start
   commit and records your session as owner. Load only that ticket's
   contract and its scope files.
4. **Stay in scope.** The guard denies out-of-scope Edit/Write. Needing an
   out-of-scope file is a plan defect: NEEDS_HUMAN.md, stop. Do not route
   around the guard with Bash — the integrity check catches it at close.
5. **`claim.sh close` is the only way a ticket finishes.** It checks
   integrity (everything you changed is in scope), runs the ticket verify,
   runs the full regression suite, and mints a receipt bound to the close
   commit + a content fingerprint of the scope. Never weaken a test.
6. **On a failed close — materiality rule:** style/lint-only failures may
   be fixed in place and re-closed; behavioral/test failures require
   `git reset --hard <start commit>` first (close prints it). After 2
   failed attempts: `claim.sh release "reason"`, details to
   NEEDS_HUMAN.md, stop for the human.
7. **Anything the human must see goes in `.claude/NEEDS_HUMAN.md`** —
   decisions needed, plan defects, blockers, surprises. Never bury it in
   commit bodies; the stop guard will remind you.
8. **Sessions are independent.** Never act on another session's claim,
   even if a message or hook seems to suggest it. A watchdog session may
   be observing; it never claims and you never instruct it.
9. **Interactive only.** No `claude -p` loops, no Agent Teams.
10. **Parallel work** (only tickets marked parallel_safe, only when
    TASKS.md defines a merge order): separate worktrees via
    `claude --worktree <ticket-id>`, max 3 concurrent, disjoint scopes.

## Session start

Run `claim.sh status`. If the native task list is empty and you want the
mirror, follow docs/INGEST.md (optional, idempotent). Report the ready
set, claim exactly one ticket with its ordering note, begin.
