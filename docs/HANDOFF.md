# Handoff — cxforge build session

Written 2026-08-15. Replaces the pre-T-31 handoff; that version described the **v1**
harness (`.claude/hooks/claim.sh`, `claim_lookup.py`, `verify_gate.sh`, the
`.claude/active-ticket` ledger) which no longer exists. If you need its Zendesk
OAuth/PKCE notes, read it out of git history rather than assuming any of its
harness instructions still apply.

## Read this in the right order

1. `.claude/rules/harness-protocol.md` — the lifecycle. It wins over everything here.
2. `.claude/NEEDS_HUMAN.md` — **D0 through D7.** This is where the build is actually
   stuck. Read it before planning anything.
3. This file — state, traps, and what to do first.
4. `docs/SPEC.md` / `docs/DESIGN.md` / `docs/tickets.json` — the plan (read-only).

## Where things stand

- **HEAD `c71b141`**, tree clean apart from `.claude/monitor/heartbeat.jsonl`.
- **13 receipts**, all fingerprint-bound: T-12, T-13, T-14, T-15, T-16, T-17, T-18,
  T-20, T-23, T-24, T-25, T-30, T-31. **19 tickets queued.**
- **`uv run pytest -m "not live" -q` → 589 passed**, 0 failed (as of T-30's close).
  It was 206 failed / 343 passed at the start of this session.
- Pushed and verified by reading back from both hosts, both at `c71b141`:
  - GitLab (cohort submission): `https://labs.gauntletai.com/hankholcomb/cxforge` → `main`
  - GitHub (private backup): `https://github.com/HankH18/cxforge` → `main`
  - Both remotes are configured (`gitlab`, `github`); local branch is `master`, pushed
    as `master:main`. Credentials resolve from the keychain / `gh` — never paste a token.

Run `bash .claude/scripts/claim.sh` (no args) for the live status board. Status is
DERIVED — receipt = resolved, claim = in_progress, else queue. The `status` field
inside `docs/tickets.json` is DEAD; nothing reads it (`backend/tests/plan/test_status_field.py`
proves this both statically and dynamically). Do not hand-maintain it.

## What this session did

**T-31 — the harness-sync migration.** Commit `c44f9af` ("cc-factory: harness sync")
swapped the v1 harness for the v2 python one and deleted `claim_lookup.py`,
`verify_gate.sh`, `claim.sh` and the `.claude/active-ticket` ledger while keeping every
test that bound them — 206 failures. All ~1700 lines of hook tests were re-expressed
against the v2 contract; no coverage was deleted, and each rewritten test's docstring
names the v1 behaviour it replaces. Migration policy for the 18 orphaned v1 receipts is
in `.claude/evidence-v1/README.md`: they are **inert legacy records**, never honoured,
never upgraded (fabricating a commit/fingerprint from a bare epoch is forbidden).

**T-12/13/14/15/16/17/18/20 — reminted** through the real lifecycle. This is what the
auditor's "AUDIT BLOCKED — `.claude/evidence/` is empty" complaint wanted.

**T-25, T-23, T-24, T-30 — real work.** Three genuine defects, all sabotage-verified:

- `backend/tests/evals/test_no_docs_writes.py` spawned a child pytest that inherited
  `OTHRAM_TEST_SCHEMA` and then, at its own teardown, `DROP SCHEMA … CASCADE`'d the
  schema the **still-running parent suite** was using. Masked because `get_connection()`
  re-issues `CREATE SCHEMA IF NOT EXISTS` on every connect — the name reappears
  instantly, empty. Fixed by giving the child its own stripped environment.
- T-16's "two concurrent runs both pass, demonstrated" was a **sequential in-process
  proxy** whose own docstring disclaimed being the demonstration. Replaced with two
  genuinely simultaneous subprocess runs (`backend/tests/data/test_concurrency.py`).
- T-20's schema-convergence check compared only name/type/nullability, so a diverged
  column DEFAULT or array element type passed silently. Proven blind first, then deepened.

## Traps — these cost real time, don't rediscover them

- **`PYTEST_CURRENT_TEST` is not set during `pytest_sessionstart`/`sessionfinish`.**
  T-24's own ticket text suggests it as the test-context signal; using it would have
  broken schema create/drop, which happen in exactly those hooks. `backend/src/data/db.py`
  gates on `PYTEST_VERSION` instead, with the reasoning written out in
  `_running_under_pytest`'s docstring. pytest is pinned `>=8.3`; `PYTEST_VERSION` arrived
  in 8.1, so the floor is safe.
- **`docs/TASKS.md` is a close-boundary artifact.** `cmd_close` removes the claim and
  *then* regenerates it, so while a ticket's own verify runs, the committed file is one
  status marker behind. `backend/tests/plan/test_tasks_md_sync.py` accepts either the
  live or the boundary rendering for this reason. Any drift beyond that still fails.
- **A new top-level file under `backend/tests/` that imports `data.db` breaks four
  already-closed tickets.** `test_blast_radius.py` puts it in T-1/T-8/T-20/T-24's
  reverse-dependency set, but their frozen verify strings don't list it. Put such files
  inside an existing suite directory (`backend/tests/data/`) instead of at the top level.
- **`test_concurrency.py` fails if a *third* pytest process hits the same database.**
  Its two children compete for connections and one exits non-zero. Passes reliably alone
  (verified across four full runs). Don't run the suite while a subagent runs it too —
  that produced a false "regression" once already.
- **ruff's `extend-exclude = ["portal", ".venv"]` is gitignore-style**, so it silently
  skips any directory named `portal` at any depth, including `backend/src/portal` and
  `backend/tests/portal`. Lint those explicitly.
- **A concurrent monitor session writing `docs/tickets.json` mid-ticket fails your
  close** with `INTEGRITY FAIL`. Handled by `release` then immediate `claim`, so the
  amendment lands in a fresh `ticket-start:` commit. See D3.
- **Subagents share this session's id**, so the scope guard applies to them and a
  subagent running `claim.sh close` will close *your* claim. Tell them not to touch the
  lifecycle.

## Environment

DB: `othram-db` container (docker compose). Suite: `uv run pytest -m "not live" -q`
(~2.5 min). Portal: `portal/node_modules` healthy at ~134 entries; an interrupted
`npm ci` leaves it broken with exit 127. `uv run python -m evals.report` exits non-zero
**by design** — the approval gate — and since T-25 it also refuses to write anywhere
under `docs/` while unapproved; pass `--output-dir` somewhere else for a draft.

## Start here next session

Everything reachable without a human decision is done. Mechanically, per ticket:

| Blocker | Tickets |
|---|---|
| `LINT` — verify string fails the harness lint | T-0, T-19 (and T-9, also dep-blocked) |
| `DEPS` — waiting on T-0's chain | T-1, T-2, T-3, T-4, T-5, T-6, T-7, T-8, T-10, T-11, T-21 |
| `PROTECTED-SCOPE` — scope guard denies its own scope | T-22, T-26, T-27, T-28, T-29 |

**The single highest-value action is D2.** T-0, T-9 and T-19 carry
`cd portal && npm run build && npm test`, which the harness lint rejects; `claim` refuses
them outright, and T-0 is the dependency root for T-1…T-11. Rewriting those three strings
as `(cd portal && …)` — the parenthesised form the lint itself recommends — frees eleven
tickets. The exact command is in `.claude/NEEDS_HUMAN.md` under D0. It cannot be run from
inside a session: `docs/tickets.json` is blocked both by the project's `scope_guard.sh`
hook and, independently, by Claude Code's own permission classifier.

Two things will bite whoever first claims **T-0**, because nobody has been able to claim
it yet and its verify is the widest in the plan:
1. `uv run ruff check .` reports **100 errors — 95 in `.claude/scripts/harness_lib.py`,
   5 in `gen_tasks.py`, and zero in product code.** The cc-factory harness files were
   added without meeting this repo's own lint config. Either reformat them (the 226 hook
   tests exercise `harness_lib.py` heavily and will catch a break) or add `.claude` to
   `extend-exclude` in `pyproject.toml`, which is T-0's own scope.
2. `uv run mypy backend` reports **4 errors** — 3 in
   `backend/tests/data/test_schema_isolation_inheritance.py`, 1 in
   `backend/tests/hooks/test_verify_gate.py`. Neither file is covered by any current
   ticket's verify, which is why they went unnoticed.

T-22, T-28 and T-29 are **substantively satisfied by the v2 harness** but unclosable as
written, because their acceptance criteria name deleted v1 files. Recommend marking them
satisfied-by-supersession rather than reimplementing v1 mechanics — reasoning and the
supporting tests are listed in D7. **T-27 is different: it has three real surviving
defects**, including a security-relevant one where v1's stop guard failed *closed* on an
unidentifiable session and v2 fails *open*.

T-7, T-10, T-11 and T-26 need you personally — label approval, a live Zendesk round trip,
a droplet, and a plan-history ruling respectively. See D4.
