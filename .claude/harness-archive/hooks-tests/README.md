# Archived: the harness hook/guard test suite

**Retired 2026-08-17 by `docs/DECISIONS.md` ADR-019. Nothing here is deleted, and nothing
here runs.** These 14 files were `backend/tests/hooks/**` and contributed 326 tests to the
gated suite. They were moved, not removed — `git log --follow` on any file reaches its full
history.

## What they tested

The cc-factory ticket harness: `.claude/hooks/scope_guard.sh`, `stop_guard.sh`, the verify
gate, and `.claude/scripts/harness_lib.py` — claim format, claim-ledger integrity, evidence
binding, append-only scope enforcement, fail-closed behaviour, and the claim/close/receipt
lifecycle. They drove the real shell scripts as subprocesses against synthetic fixture
projects, which is why they were good tests of a thing that no longer happens.

## Why they were retired

`ADR-001` retired the ticket lifecycle, and **W0.2 removed the `PreToolUse` scope-guard, the
`Stop` stop-guard and the task gate from `.claude/settings.json` outright.** Only the
`PostToolUse` heartbeat hook is still registered. So these tests exercise scripts that
nothing invokes: the coverage is real, but its subject is dead code.

The trigger was CI. `test_scope_guard_fail_closed.py::test_guard_denies_when_python3_is_unavailable`
had failed **30 consecutive runs — every run in the workflow's recorded history, zero
successes.** It simulates "the helper cannot run" by setting `PATH=/bin`, on the stated
premise that `/bin` "carries bash but not python3". That is true on macOS and **false on the
Ubuntu runner**, where `/bin` is a symlink to `/usr/bin` under usrmerge — so `python3` is
found, the guard runs correctly, allows an in-scope write, and the assertion reads that
correct allow as a failure to fail closed (`assert 'allow' == 'deny'`). The test was wrong
about its environment, not about the guard. Fixing it would have meant maintaining a portable
way to hide an interpreter from a script that no longer runs in the first place.

ADR-019 originally retired only three tests from this directory and deliberately kept the
other 326; the CI history is what changed the answer. See ADR-019 for that reasoning.

## What this is not

This is **not** weakening a test to make failing code pass, which `build-protocol.md` rule 7
forbids and which remains forbidden. No product code was failing these tests. They were
retired because the process they verify has ended, by an explicit owner decision — the same
treatment ADR-001 gave `.claude/evidence/` and ADR-018 gave `backend/tests/plan/**`.

## If the harness is ever revived

Move this directory back to `backend/tests/hooks/` and it works again; nothing in the test
files was edited. Two things must come back with it: the guard hook registrations in
`.claude/settings.json` (without them, the three tests in
`../retired_settings_json_tests.py` are what assert they exist), and a fix for the
`PATH=/bin` assumption above if you want it green on Linux.

One outside reference moved with it:
`backend/tests/test_skip_db_tests_relocation.py` used this directory as its "unrelated
directory is unaffected by `SKIP_DB_TESTS=1`" sample and now uses `backend/tests/contract`
instead. That guard asserts nothing about the harness, so it needs no change if the harness
returns.
