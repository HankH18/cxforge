# Archived: the plan-integrity test suite

**Retired 2026-08-16 by `docs/DECISIONS.md` ADR-018. Nothing here is deleted, and nothing
here runs.** These 12 files were `backend/tests/plan/**` and contributed 92 tests to the
gated suite. They were moved, not removed — `git log --follow` on any file reaches its full
history.

## What they tested

The **plan**, not the software. Every one of them gated on `docs/tickets.json`,
`docs/TASKS.md`, or `.claude/` — the cc-factory ticket harness that `ADR-001` retired.

The most substantial was `test_blast_radius.py`, and its rule was a good one: *if a ticket's
scope can touch a source package, that ticket's `verify` command must run every test suite
which depends on that package.* Otherwise a ticket could edit `data/`, run only
`tests/data/`, and never learn that `tests/portal/` imports `data/` too. It enforced this by
building an import graph of the whole repo on every run.

## Why they were retired

Two reasons, and the second is the one that matters.

**1. They constrained new work using frozen history.** The suite read all 32 tickets with
**no filter for status**, so the immutable `verify` strings of 30 *closed* tickets decided
where new code could live. Adding any test directory that imported `main` turned 11 closed
tickets red — not because anything was broken, but because a ticket that closed weeks ago
could not have named a directory that did not exist yet. Three of the four Wave 1 tracks hit
this. Track A's plan specified `backend/tests/worker/`, which was caught before it was
written; Track F actually hit it and had to restructure a deploy test around it.

Worse, the check had gone **blind to the thing it most needed to see**: `worker` is in
neither `FIRST_PARTY_ROOTS` nor `KNOWN_PACKAGES`, so the graph could not see
`ingress → worker` — the single most important new dependency in the repo. 92 green tests,
not looking at it.

**2. The gate subsumes them.** `test_blast_radius.py` existed to make a ticket's *narrow*
verify command sufficient. ADR-001 replaced narrow per-ticket verifies with **the full suite
before every commit** (`.claude/rules/build-protocol.md` rule 2, ADR-016). With no tickets
and a full-suite gate, there is nothing left for it to protect. Retiring it costs **zero**
coverage of the product.

## What this is not

This is **not** an example of weakening a test to make failing code pass, which
`build-protocol.md` rule 7 forbids and which remains forbidden. These tests were passing.
They were retired because the process they verify has ended, by an explicit owner decision
recorded as an ADR — the same treatment ADR-001 gave `.claude/evidence/`.

## If the harness is ever revived

Move this directory back to `backend/tests/plan/` and it works again; nothing about it was
edited. `update_structural_snapshot.py` still regenerates
`ticket_structural_snapshot.json` and reads only `docs/tickets.json`.

## The sibling suite — retired too, one day later

`backend/tests/hooks/**` also tested harness machinery, and ADR-018 left it in the gated
suite pending its own decision. That decision came on 2026-08-17: it is now
`../hooks-tests/`, see the amendment at the end of `docs/DECISIONS.md` ADR-019.
