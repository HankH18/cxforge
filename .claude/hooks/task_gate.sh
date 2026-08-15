#!/usr/bin/env bash
# Delegates to harness_lib.py's hook-taskgate (PreToolUse[TaskUpdate]/TaskCompleted
# guard; refuses to mark a T-<n> task complete without a receipt). Protocol (T-22):
# ticket status is DERIVED, never stored, so no ticket boundary needs an agent-side
# Edit/Write of docs/tickets.json or docs/TASKS.md -- cmd_close regenerates TASKS.md
# itself.
#
# T-29: what the receipt this gate keys off actually binds, and what it does not.
# A `.claude/evidence/<id>.json` receipt (the only thing `receipt()` reads -- see
# harness_lib.py's `cmd_hook("taskgate")`) is minted by `cmd_close` in this exact
# order: (1) integrity check that everything changed since the claim's start_commit
# is in scope, (2) run the ticket's `verify` (and the cross-ticket `full_verify`, if
# the plan defines one), (3) on a pass, `git add -A && git commit -m
# "ticket-close: <id>"`, (4) THEN compute `fingerprint` (a sha256 over the ticket
# scope's tracked file bytes) and write it, plus `commit` (the just-made HEAD) and
# `ts` (the epoch at write time), into the receipt.
# In short: the binding certifies the tree the verify ran on, not that that tree
# was committed as a matter of independent proof -- it IS the commit, by
# construction of step (3), but that guarantee has a precise boundary:
#   - What this proves: `receipt.commit` is the real commit the verify-passing tree
#     was captured into, and `receipt.fingerprint` is a content hash of that same
#     tree's scope files, computed immediately after the commit (git commit does not
#     touch the working tree, so the bytes fingerprinted are exactly the bytes just
#     committed). A completion-titled commit and the tree the gate verified can no
#     longer silently diverge the way v1's bare-epoch `.pass` allowed (T-12's gate
#     closed 33 minutes and one commit after its completion commit; only timestamp
#     forensics could reconstruct which tree passed -- see T-29's objective).
#   - What this does NOT prove: that the tree stays that way going forward (nothing
#     stops a later `git reset`/force-push from discarding the close commit), or that
#     no external process mutated a tracked scope file in the narrow window between
#     step (2)'s verify finishing and step (3)'s `git add -A` capturing the tree --
#     verify running strictly BEFORE the commit means the receipt binds to whatever
#     was on disk right before that commit, not to a live snapshot taken the instant
#     verify itself read the files. A verify command (or a `full_verify`) with file
#     side effects, or a second writer racing the close, is exactly this gap; nothing
#     in the harness detects or flags it today (see .claude/NEEDS_HUMAN.md, T-29
#     acceptance 1 finding).
exec python3 "${CLAUDE_PROJECT_DIR}/.claude/scripts/harness_lib.py" hook-taskgate
