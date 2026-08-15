# Build Execution Protocol — othram-support-agent

This repo has an approved build plan: docs/SPEC.md (intent),
docs/DESIGN.md (contracts), docs/tickets.json (the task graph —
authoritative; docs/TASKS.md is its human-readable mirror). Your job
is execution, not planning.

## Rules

1. **Do not enter plan mode. Do not re-plan or redesign.** The task
   graph is the approved plan. If the plan is wrong (discovered
   reality — e.g. the Zendesk trial blocks webhooks, OAuth scope
   surprises), STOP and tell the human — do not push through and do
   not silently rewrite tickets.
2. **One ticket at a time.** Pick a ready Task (pending, unowned, no
   open blockers) via TaskList. Claim it: TaskUpdate owner + status
   in_progress, and write its ticket ID (e.g. T-5) as the only line
   of `.claude/active-ticket`.
3. **Load only what the ticket needs**: its contract (Task
   description) and the files in its scope. Do not read the whole
   graph or other tickets' contracts.
4. **Stay inside the ticket's file scope.** A hook denies edits
   outside it. If the ticket genuinely requires touching an
   out-of-scope file, that's a plan defect — stop and tell the human.
5. **A ticket is done only when its verify command exits 0.** A hook
   runs it when you mark the Task completed and blocks completion on
   failure. Never weaken a test to pass it.
6. **Commit at every ticket boundary**: one commit immediately after
   claiming (marks ticket start), one on completion, message
   "T-x: <title>". /rewind does not undo bash side effects — the
   start commit is the durable revert point.
7. **On failed verify: revert, don't iterate.** `git reset --hard` to
   the ticket-start commit, then retry from clean state. After 2
   failed attempts, stop, summarize both failures, and ask the human.
8. **Interactive only.** Do not suggest or script `claude -p` loops.
9. **Parallel work**: worktrees via `claude --worktree <ticket-id>`,
   max 3 concurrent, only tickets marked parallel_safe in
   docs/tickets.json with disjoint scopes. Merge order: ascending
   ticket ID (stated in docs/TASKS.md).

## Design invariants (do not "improve" these away)

- Case facts reach outbound replies only via templates filled from
  tool results — never free generation (SPEC R9).
- Gated/approved sends count as human-touched in metrics (SPEC R12).
- OAuth only for Zendesk — never API tokens.
- Do not relax eval thresholds (T-7) to make a ticket pass.

## Human-only steps — surface and WAIT, never attempt

- Zendesk trial signup, OAuth app creation, credential entry
- DigitalOcean account actions
- T-7 label approval (external ground truth — never self-approve)
- Demo video recording

T-10 (`pytest -m live`) cannot pass until the human has completed
docs/zendesk-runbook.md. If credentials are absent, say so and wait.

## Session start

If TaskList is empty for this project, follow docs/INGEST.md first.
Then: report which tickets are ready, claim exactly one, begin.
