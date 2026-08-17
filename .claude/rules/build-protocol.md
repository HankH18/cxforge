# Build protocol — the last mile

**Current as of 2026-08-16. Supersedes `.claude/rules/harness-protocol.md`**, which is
retired by `docs/DECISIONS.md` ADR-001 and kept only as historical record.

> **Transition note.** The scope-guard / stop-guard / task-gate hooks are still installed
> in `.claude/settings.json` until work package **W0.2** removes them. Until then they may
> still deny writes to `docs/tickets.json`, `docs/SPEC.md`, `docs/DESIGN.md`,
> `.claude/hooks/**` and `.claude/scripts/**`. That is expected, not a bug. Do not route
> around a guard; do W0.2 first.

## Read these, in this order

1. `docs/STATE.md` — what is actually true today. Verified, not narrated.
2. `docs/BUILD-PLAN.md` — the work, the waves, the frozen contracts, the ownership matrix.
3. `docs/DECISIONS.md` — why each choice was made, and what it commits us to.
4. `docs/OWNER-ACTIONS.md` — what is blocked on the human.
5. `docs/SPEC.md` / `docs/DESIGN.md` — the requirements and the contracts.

`docs/HANDOFF.md` and `.claude/NEEDS_HUMAN.md` are **historical**. They contain accurate
records of solved problems and stale claims about live ones. Do not act on either without
checking the claim against the code first.

## Rules

1. **No ticket lifecycle.** There is no claim, no close, no receipt. Do not run
   `.claude/scripts/claim.sh`. `.claude/evidence/` is a frozen historical record of the 30
   tickets that did go through the harness — never write to it, never delete it.

2. **The gate is the suite, and it does not move.** Before every commit:
   ```bash
   uv run pytest -m "not live" -q
   uv run ruff check .
   uv run ruff check backend/src/portal backend/tests/portal   # see rule 3
   uv run mypy backend
   cd portal && npm run build && npm test                      # when portal/** changed
   ```
   A red suite is not committed. "Unrelated failure" is a claim that needs evidence.

3. **`ruff`'s `extend-exclude` is gitignore-style**, so `["portal", ".venv"]` silently
   skips any directory named `portal` **at any depth** — including `backend/src/portal`
   and `backend/tests/portal`. Lint those explicitly or they go unchecked.

4. **One work package at a time**, and stay inside its row of the ownership matrix in
   `docs/BUILD-PLAN.md §8`. Concurrent tracks are only safe because their file sets are
   disjoint. If a package genuinely needs a file another track owns, say so and sequence
   it — do not just take the file.

5. **Contracts in `BUILD-PLAN.md §1` are frozen.** Parallel tracks are building against
   them right now. If one is wrong, stop, change it in `docs/DESIGN.md` deliberately, and
   tell the other tracks. Do not silently widen a signature.

6. **A test that passes with your change removed is not a test.** For every new behaviour,
   actually delete or break the implementation and confirm the test goes red. Reasoning
   about it does not count. This is the discipline the whole core-loop gap came from: 702
   green tests, and not one of them noticed that nothing ever called `run_agent`.

7. **Never weaken a test to make it pass.** If an existing test blocks you, the burden is
   overwhelming evidence that the *test* is wrong — not that it is failing, strict, or
   inconvenient. Failing tests usually mean the code is wrong.

8. **Live claims require reading the effect back.** "Deployed", "it's live", "the webhook
   works" are only sayable after querying the system and seeing the result. Do not infer
   success from a command's exit code — that is exactly how the droplet passed
   `verify_deploy.sh` 4/4 for weeks with no `ANTHROPIC_API_KEY` and a dead core loop.

9. **Commits are ordinary and honest.** Conventional-commit subject, body says what
   changed and what proves it. Include the suite numbers when they move. Do not assert
   authorization in a commit body — decisions belong in `docs/DECISIONS.md`.

10. **Blockers and surprises go in `docs/BUILD-PLAN.md §10` or a new ADR**, not buried in
    a commit message and not appended to the retired `.claude/NEEDS_HUMAN.md`.

11. **Anything that changes scope, contracts, or what ships is the owner's call.** Ask
    directly rather than assuming. Every decision already taken is in `docs/DECISIONS.md`;
    if the answer is not there, it has not been decided.

## Environment

- **DB:** `othram-db` container via docker compose.
- **Suite:** ~2.5 min. `backend/tests/data/test_concurrency.py` spawns two subprocesses
  and **fails if a third pytest process touches the same database** — never run the suite
  while a subagent is also running it. That has already produced one false "regression".
- **Portal:** `portal/node_modules` is healthy at ~134 entries; an interrupted `npm ci`
  leaves it broken with exit 127.
- **Env:** nothing in the app calls `load_dotenv()`. Prefix commands with
  `set -a; source .env; set +a` or they see no credentials. Work package **W1-F4** fixes
  this properly.
- **Deploy:** `docs/deploy.md:139` requires `set -a; source .env; set +a` **before**
  `docker compose … up`, or every `${VAR}` silently falls back to its compose default.
- **Evals:** `uv run python -m evals.report` exits non-zero **by design** (the approval
  gate) and refuses to write under `docs/` while unapproved. Use `--output-dir` elsewhere
  for a draft.
- **New test files: put them wherever they belong.** The old constraint here — that a new
  test directory turned up to 11 closed tickets red in `test_blast_radius.py` — is **gone
  as of `docs/DECISIONS.md` ADR-018**, which retired `backend/tests/plan/**` to
  `.claude/harness-archive/plan-tests/`. There is no longer an import graph gating on
  closed tickets' frozen `verify` strings, so no directory is cheaper than another and no
  import is dangerous. Choose placement on ordinary grounds: put the test next to what it
  tests.

  If you are reading an older doc, a code comment, or a docstring that warns about
  `test_blast_radius.py`, `_planlib.py`, `FIRST_PARTY_ROOTS`, or the cost of importing
  `main` — that warning is historical. The archived suite's README explains what it did and
  why it was retired.

- **Tests must write only under `tmp_path`.** `backend/tests/conftest.py:346-401` diffs
  `git status --porcelain` before and after the session and fails the run if any path
  outside the harness allowlist was added, modified, or removed.
