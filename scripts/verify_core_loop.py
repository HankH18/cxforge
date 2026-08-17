#!/usr/bin/env python3
"""W3-G2 — the deep deploy check: does the CORE LOOP actually run?

`scripts/verify_deploy.sh`'s existing four assertions are liveness checks.
Every one of them passes against a process that answers HTTP and does
nothing else, which is exactly why the stack passed 4/4 for weeks with **no
`ANTHROPIC_API_KEY` at all**, and why it still passes 4/4 today against a
droplet whose webhook accepts events and never starts a run
(`docs/STATE.md §6.2`). A check that cannot fail when the product is broken
manufactures confidence.

This module is the opposite. It drives the real public endpoint with a
correctly HMAC-signed body and then waits for the *effect*:

    POST /webhooks/zendesk  (real HTTP, real signature)
      -> the deployed ingress handler
      -> a real Redis broker
      -> a real arq worker
      -> agent.graph.run_agent
      -> a new `runs` row, read back through the deployed portal API

Nothing here calls a Python function of the application under test. The one
thing it *does* import from `backend/src` is `ingress.signature`, and that
is deliberate: signing here with a private copy of the HMAC recipe would let
the two drift, and this check would then be testing its own copy rather than
the server's. `docs/STATE.md §1` records why this matters — **no code in
this repo has ever opened a real Redis connection**; `ArqJobQueue` has only
ever met a stub whose `**kwargs` would silently absorb a renamed parameter.
This is the first thing that exercises that wire.

---------------------------------------------------------------------------
Why this needs a REAL helpdesk ticket id, and takes one rather than inventing
one
---------------------------------------------------------------------------
`agent.nodes.ingest` is the graph's first node and its first statement is
`deps.port.fetch_ticket(ticket_id)` against the live helpdesk. A fabricated
ticket id therefore 404s, `worker.main.run_ticket` catches the
`HelpdeskAPIError`, releases the dedup row (ADR-003) and returns — and **no
`runs` row is ever written**. A check keyed on a made-up id would fail
identically whether the core loop worked or not, which is precisely the
"cannot distinguish" defect G2 exists to remove. So `CXFORGE_VERIFY_TICKET_ID`
is required, and it must name a disposable ticket the deployed agent can
really fetch.

Per-invocation uniqueness — the thing that closes the stale-row false pass —
comes from two places instead, and both are stronger than a unique ticket id:

  1. a fresh `comment_id` every run, so the `tickets_seen` idempotency guard
     can never swallow the event as a duplicate; and
  2. a **baseline set of `run_id`s captured before the POST**. The row this
     check accepts must be a `runs` row for that ticket whose `run_id` was
     not already there. `runs.id` is a `bigserial`, so "not in the baseline"
     is monotonic proof the row is new — and unlike a timestamp comparison it
     is immune to clock skew between this machine and the deployment.

If the worker is dead but an old `runs` row for the same ticket happens to
exist, rule 2 rejects it and this check fails. That case is covered by a unit
test (`backend/tests/deploy/test_deep_check.py`).

Rule 2 proves a row is **new**. It does not prove the row is **mine** — the
ticket is reused, so a real customer comment can produce a new row inside the
same window. So there is a rule 3: **exactly one** new row for the ticket, or
this check refuses to name a winner (`find_new_run` raises `AmbiguousRun`,
exit 1, both ids printed). When `CXFORGE_VERIFY_DB_URL` is available a fourth
check corroborates that from `tickets_seen`, the only table in this schema
that records which comment triggered a run. See "Why this script never
removes a `runs` or `drafts` row" below for what a wrong answer here cost.

---------------------------------------------------------------------------
Reading the effect back, and the database this must NOT use
---------------------------------------------------------------------------
The row is read back through `GET /api/feed` on the deployment under test.
That is on purpose: it is the deployed application's own view of its own
database, over the same public entry point a human uses, and it works in
REMOTE mode where the droplet's Postgres publishes no host port at all.

A corroborating raw-row dump, the read-only provenance corroboration
described below, and the dedup-row release additionally need SQL, and that
comes from `CXFORGE_VERIFY_DB_URL` — a **separate variable that is never
defaulted from `DATABASE_URL`**. In REMOTE mode `.env`'s `DATABASE_URL` is
`postgresql://…@localhost:5432/othram`: the *developer's dev database*, not
the droplet's. Silently falling back to it would point this script's one
write at a completely different database from the one the run happened in —
the exact shape of every bug in `docs/STATE.md §6`. When it is absent this
check says so loudly and never pretends it tidied up.

---------------------------------------------------------------------------
Why this script never removes a `runs` or `drafts` row
---------------------------------------------------------------------------
It used to, and it cost real data. Until 2026-08-17 the `finally` below
issued a row-level delete against `drafts` and then `runs` for whatever run
id `find_new_run` had adopted, and `find_new_run` adopted `max(run_id)`
among the rows for the ticket that were absent from its baseline snapshot.
Two things followed from that:

  * **It destroyed production audit data.** Runs 4, 5 and 6 on the droplet
    were successful `auto_sent` runs at 10:45–10:49Z on 2026-08-17 that each
    posted a real public reply to a real Zendesk ticket. Those replies are
    still in Zendesk; the only local record of them is gone. `runs.id` and
    `drafts.id` are independent `bigserial`s and BOTH skip 4-6, and
    `agent.store.record_run` / `record_draft` each commit a single
    statement, so no rollback can account for the gap.

  * **"Newest id for this ticket that I had not seen" is not proof of
    authorship.** `CXFORGE_VERIFY_TICKET_ID` names a ticket that is
    deliberately REUSED, and a real customer comment can land on it inside
    this check's own polling window. The old code could therefore adopt —
    and then delete — a customer's audit row, and would leave no trace that
    it had.

The rule is now structural rather than careful: nothing in this module may
issue a delete, update or truncate touching `runs` or `drafts`. No such
statement exists here; `_execute` refuses to run one if a future edit
reintroduces it (`assert_not_destructive`); and
`backend/tests/deploy/test_deep_check.py` fails if any string this module
could hand to a cursor contains one. The synthetic run this check causes is
**left in place** and reported on stdout by run id.

That is a deliberate trade, not an oversight: visible pollution is
recoverable and a deleted audit row is not. What it costs, said plainly —
every invocation adds one synthetic run to `runs`, and `GET /api/metrics`
counts it (`human_avoidance_rate`, both latency percentiles,
`sample_count`). `runs` carries no provenance column, so nothing can
separate that row from a real one except the ticket id, which real
customers also use. Closing that properly needs a schema change
(`runs.source`, or the triggering `comment_id`) and that is the owner's
call, not this script's — see `docs/BUILD-PLAN.md §10`.

The one delete that remains targets `tickets_seen`, keyed on
`(ticket_id, comment_id)` where the comment_id is a `cxforge-verify-<uuid4>`
minted by THIS process moments earlier. That row is provably this
invocation's own — no inference about ids or ordering is involved — and
`tickets_seen` is a dedup guard, not an audit record.

Usage (normally invoked by scripts/verify_deploy.sh --deep):

    CXFORGE_VERIFY_TICKET_ID=12345 \
    uv run python scripts/verify_core_loop.py --base-url http://127.0.0.1:8080

Exit codes: 0 = the core loop ran and the row is real. 2 = a precondition is
missing (nothing was sent). 1 = the check ran and FAILED.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]

# `backend/src` is on `pythonpath` for pytest only (pyproject.toml), and
# `[tool.uv] package = false` means nothing is installed either — so a script
# run as `uv run python scripts/…` has to put it there itself.
if str(REPO_ROOT / "backend" / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "backend" / "src"))

from ingress.signature import compute_signature  # noqa: E402

WEBHOOK_PATH = "/webhooks/zendesk"
FEED_PATH = "/api/feed"
GATE_PATH = "/api/settings/gate"

# The gap between `received_at` and `replied_at` that a *real* run produces.
# ADR-004 exists because the pre-Wave-1 code minted `received_at` inside
# `act` — the LAST graph node — so the interval timed only the tail-end
# helpdesk calls and measured **22µs** across a 300ms ingest delay. Anything
# that small is not a run; it is the old defect, back. 0.5s is far below any
# real run (3+ model calls) and far above 22µs, so it separates the two
# without encoding a performance expectation.
MIN_PLAUSIBLE_RUN_SECONDS = 0.5

POLL_INTERVAL_SECONDS = 2.0
DEFAULT_TIMEOUT_SECONDS = 240.0

# Outcomes that mean the agent finished and answered. A run held by the
# approval gate is `outcome = NULL` / `replied_at = NULL` by design
# (`agent.nodes.act`), which is why the gate is checked before anything is
# sent rather than diagnosed afterwards from a null column.
TERMINAL_OUTCOMES = ("auto_sent", "escalated", "off_topic")


class PreconditionMissing(Exception):
    """A required input is absent. Nothing has been sent; exit 2."""


class DeepCheckFailed(Exception):
    """The check ran and the deployment did not satisfy it; exit 1."""


class AmbiguousRun(DeepCheckFailed):
    """More than one candidate `runs` row, and no way to tell them apart.

    A `DeepCheckFailed` subclass so `main` treats it as an ordinary failure
    (exit 1), and a distinct type so the ambiguity can be asserted on
    precisely instead of by matching a message. `candidate_run_ids` carries
    every id that was in contention, because the caller has to name them on
    stdout: one of them may be a real customer's run.
    """

    def __init__(self, message: str, candidate_run_ids: tuple[int, ...]) -> None:
        super().__init__(message)
        self.candidate_run_ids = candidate_run_ids


class ForbiddenStatement(RuntimeError):
    """A statement was about to be executed that could destroy audit rows.

    Raised by `assert_not_destructive`, i.e. inside `_execute`, not at
    build time — the point is to sit between ANY future caller and the
    database, including one that composes its SQL somewhere this module's
    static checks cannot see it.
    """


# -- request construction ---------------------------------------------------


def synthetic_comment_id() -> str:
    """A comment id no previous invocation can have used.

    Prefixed so an operator reading `tickets_seen` on a live deployment can
    tell instantly that the row came from a verification run and not from a
    real customer comment.
    """
    return f"cxforge-verify-{uuid.uuid4().hex}"


def build_signed_request(
    *,
    secret: str,
    ticket_id: str,
    comment_id: str,
    requester_email: str = "verify@cxforge.invalid",
) -> tuple[bytes, dict[str, str]]:
    """Return the exact bytes to POST and the headers that sign *those bytes*.

    Returned as bytes rather than a dict on purpose. `ingress.signature`'s
    module docstring is explicit that Zendesk signs `timestamp + RAW body`,
    and that re-serializing before hashing changes the bytes (key order,
    whitespace, unicode escaping) and breaks verification against a real
    webhook. So this builds the body once and both signs and sends that same
    buffer — a caller cannot accidentally re-encode it in between.
    """
    payload = {
        "ticket_id": ticket_id,
        "comment_id": comment_id,
        "requester_email": requester_email,
        "subject": "cxforge deploy verification",
        "latest_comment_text": (
            "This is an automated deploy verification message from "
            "scripts/verify_core_loop.py. What is the current status of my case?"
        ),
    }
    # `separators` pins the encoding so the bytes are stable and minimal; the
    # signature is computed over whatever this produces, so the exact form
    # does not matter — only that it is computed once.
    raw_body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {
        "Content-Type": "application/json",
        "X-Zendesk-Webhook-Signature-Timestamp": timestamp,
        "X-Zendesk-Webhook-Signature": compute_signature(secret, timestamp, raw_body),
    }
    return raw_body, headers


# -- pure decision logic (unit-tested in backend/tests/deploy) ---------------


def find_new_run(
    feed: list[dict[str, Any]], *, ticket_id: str, baseline_run_ids: set[int]
) -> dict[str, Any] | None:
    """The run this invocation caused, or None if it has not appeared yet.

    Both conditions are load-bearing. Matching on `ticket_id` alone would
    accept a `runs` row left behind by an earlier invocation — a dead worker
    plus one stale row would read as a pass, which is the exact failure mode
    this check exists to make impossible.

    **Exactly one candidate, or this raises.** `runs` carries no provenance
    (no `source`, no originating `comment_id`), so the only attribution
    available is by elimination: one row for this ticket that was not in the
    pre-POST baseline, and this invocation is the one thing known to have
    just posted to this ticket. The moment there are two, elimination is
    over — and because `CXFORGE_VERIFY_TICKET_ID` names a deliberately
    REUSED ticket, the second one can be a real customer's run that arrived
    inside the polling window.

    The predecessor of this function returned `max(run_id)` here. It called
    that "newest first, so a (pathological) double-run reports the later
    row"; what it actually did was hand a stranger's run id to a `finally`
    that deleted it. Guessing the newest is how audit rows 4-6 were lost.
    An `AmbiguousRun` that stops the check is the cheap outcome; a confident
    wrong answer is the expensive one.
    """
    candidates = [
        item
        for item in feed
        if item.get("ticket_id") == ticket_id and item.get("run_id") not in baseline_run_ids
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        ids = tuple(sorted(int(item["run_id"]) for item in candidates))
        raise AmbiguousRun(
            f"{len(candidates)} new runs rows appeared for ticket {ticket_id} during this "
            f"check's window: {list(ids)}. This invocation posted ONE webhook, so at most "
            "one of them is mine and at least one belongs to something else — most likely a "
            f"real customer comment on ticket {ticket_id}, which is reused on purpose. "
            "`runs` records no provenance, so there is no way to tell which is which from "
            "here. Refusing to pick one: attributing the wrong row is how this check came "
            "to delete production audit rows 4-6. Nothing has been deleted; inspect these "
            "ids by hand.",
            ids,
        )
    return candidates[0]


def assert_row_is_real(run: dict[str, Any], *, ticket_id: str) -> float:
    """Raise unless the row records a genuine, completed run. Returns the
    measured `replied_at - received_at` interval in seconds.

    This is where ADR-004 is enforced against the deployment rather than
    against a unit test: before Wave 1 the clock was minted in the last node
    and the interval came out at 22µs while a whole run had happened around
    it. A row can exist and still be evidence of nothing.
    """
    if run.get("ticket_id") != ticket_id:
        raise DeepCheckFailed(
            f"runs row is for ticket {run.get('ticket_id')!r}, expected {ticket_id!r}"
        )
    if not run.get("route"):
        raise DeepCheckFailed(f"runs row {run.get('run_id')} has no route — classify never ran")
    outcome = run.get("outcome")
    if outcome not in TERMINAL_OUTCOMES:
        raise DeepCheckFailed(
            f"runs row {run.get('run_id')} has outcome {outcome!r}; expected one of "
            f"{TERMINAL_OUTCOMES}. A null outcome means the approval gate held the draft "
            "pending — turn the gate OFF and re-run, or this check cannot see a reply."
        )
    received_at, replied_at = run.get("received_at"), run.get("replied_at")
    if not received_at or not replied_at:
        raise DeepCheckFailed(
            f"runs row {run.get('run_id')} has received_at={received_at!r} "
            f"replied_at={replied_at!r}; both must be populated for the latency "
            "interval R13 reports to mean anything (ADR-004)."
        )
    started = datetime.fromisoformat(received_at)
    replied = datetime.fromisoformat(replied_at)
    interval = (replied - started).total_seconds()
    if interval < MIN_PLAUSIBLE_RUN_SECONDS:
        raise DeepCheckFailed(
            f"runs row {run.get('run_id')} spans only {interval * 1e6:.0f}µs "
            f"({interval:.6f}s). ADR-004 requires received_at to be stamped at webhook "
            "receipt so the interval covers the WHOLE run; an interval this short means "
            "the clock is being minted late again (the 22µs defect), not that the agent "
            "is fast."
        )
    return interval


# -- HTTP ------------------------------------------------------------------


def _feed(client: httpx.Client, base_url: str, portal_token: str) -> list[dict[str, Any]]:
    response = client.get(
        f"{base_url}{FEED_PATH}", headers={"X-Portal-Token": portal_token}, timeout=20.0
    )
    if response.status_code != 200:
        raise DeepCheckFailed(
            f"GET {FEED_PATH} returned {response.status_code}, expected 200 — the check "
            "cannot read the run back. Is PORTAL_TOKEN the one this deployment was "
            "started with?"
        )
    payload = response.json()
    runs = payload.get("runs")
    if not isinstance(runs, list):
        raise DeepCheckFailed(f"GET {FEED_PATH} returned an unexpected shape: {payload!r}")
    return runs


def _assert_gate_is_off(client: httpx.Client, base_url: str, portal_token: str) -> None:
    response = client.get(
        f"{base_url}{GATE_PATH}", headers={"X-Portal-Token": portal_token}, timeout=20.0
    )
    if response.status_code != 200:
        raise DeepCheckFailed(f"GET {GATE_PATH} returned {response.status_code}, expected 200")
    if response.json().get("enabled"):
        raise DeepCheckFailed(
            "the approval gate is ON, so this run would be held as a pending draft with "
            "replied_at NULL and no outcome (agent.nodes.act). Turn it off "
            f"(PUT {GATE_PATH} {{\"enabled\": false}}) and re-run — this check asserts a "
            "reply actually went out, and refuses to weaken that assertion instead."
        )


# -- what this script is not allowed to do ---------------------------------

# The audit tables. A `runs` row is the only local record that the agent
# answered a real customer, and a `drafts` row is the text it sent. Losing
# either is unrecoverable from inside this system: `docs/BUILD-PLAN.md §10`
# records three replies that are still visible in Zendesk and have no local
# trace at all, because this script deleted them.
PROTECTED_TABLES = ("runs", "drafts")

# Built from PROTECTED_TABLES rather than spelled out, for a small reason
# that matters: it keeps the literal table names out of this pattern, so the
# static scan in `backend/tests/deploy/test_deep_check.py` — which greps
# every string constant in this module for destructive SQL against those
# tables — does not flag its own guard.
_DESTRUCTIVE_SQL = re.compile(
    r"\b(?:DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|UPDATE)\s+(?:ONLY\s+)?\"?"
    rf"(?:{'|'.join(PROTECTED_TABLES)})\b",
    re.IGNORECASE,
)


def assert_not_destructive(statement: str) -> None:
    """Raise `ForbiddenStatement` if `statement` could destroy an audit row.

    Belt to the braces of "there is no such statement in this file". The
    static test can only see string literals; this sees whatever is actually
    about to reach a cursor, however it was composed. It is deliberately
    coarse — a false positive here costs a developer one confused minute,
    and a false negative costs an audit trail.
    """
    if _DESTRUCTIVE_SQL.search(statement):
        raise ForbiddenStatement(
            "refusing to execute a statement that mutates or removes rows in "
            f"{' / '.join(PROTECTED_TABLES)}: {statement!r}. This script is a deploy "
            "gate; it may read the audit tables and it may never change them. The "
            "verification run it causes is left in place and reported on stdout "
            "instead — see this module's docstring for the three production replies "
            "that were lost when it did otherwise."
        )


# -- cleanup ---------------------------------------------------------------
#
# One statement, against one table that is not an audit record, keyed on a
# value this process minted itself. Nothing here is inferred from ids,
# ordering or recency.


def dedup_cleanup_statements(ticket_id: str, comment_id: str) -> list[str]:
    """The exact SQL that releases this invocation's dedup row, as text.

    `tickets_seen` is keyed on `(ticket_id, comment_id)`, and `comment_id`
    is the `cxforge-verify-<uuid4>` this process generated seconds earlier
    (`synthetic_comment_id`), so the row addressed here is provably the one
    this invocation caused. That is the standard the removed `runs`/`drafts`
    deletes could not meet, and the reason this one survives while they did
    not: it needs no argument about which row is whose.

    Deliberately takes no `run_id`. The parameter's only purpose used to be
    to widen this list to the audit tables.
    """
    return [
        f"DELETE FROM tickets_seen WHERE ticket_id = '{ticket_id}' "
        f"AND comment_id = '{comment_id}';"
    ]


def _execute(db_url: str, statements: list[str]) -> str:
    import psycopg

    # Checked for ALL statements before any of them runs, so a list that is
    # destructive in its third element cannot get its first two committed.
    for statement in statements:
        assert_not_destructive(statement)

    counts = []
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        for statement in statements:
            cur.execute(statement.encode())
            counts.append(f"{cur.rowcount}")
    return ", ".join(f"{s.split()[2]}={c}" for s, c in zip(statements, counts, strict=True))


def unexpected_comment_ids(seen_now: set[str], *, baseline: set[str], ours: str) -> set[str]:
    """Comment ids that reached this ticket during the window and are not ours.

    A non-empty result means something other than this invocation was
    accepted on `CXFORGE_VERIFY_TICKET_ID` while the check was polling — the
    exact circumstance under which "the one new run for this ticket must be
    mine" stops being true. `tickets_seen` is the only table in this schema
    that records the originating comment id at all, which is why the
    corroboration hangs off it rather than off `runs`.

    Pure so it can be tested without a database; the read that feeds it
    (`_seen_comment_ids`) is a plain SELECT.
    """
    return {comment_id for comment_id in seen_now - baseline if comment_id != ours}


def _seen_comment_ids(db_url: str, ticket_id: str) -> set[str]:
    """Read-only. Every `tickets_seen.comment_id` currently on this ticket."""
    import psycopg

    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT comment_id FROM tickets_seen WHERE ticket_id = %s", (ticket_id,))
        return {str(row[0]) for row in cur.fetchall()}


def _dump_run(db_url: str, run_id: int) -> str:
    import psycopg

    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, ticket_id, route, confidence, outcome, verifier_score, "
            "trace_id, received_at, replied_at, reasons FROM runs WHERE id = %s",
            (run_id,),
        )
        columns = [d.name for d in cur.description or []]
        row = cur.fetchone()
    if row is None:
        return "      (no such row — the feed and the database disagree!)"
    pairs = zip(columns, row, strict=True)
    return "\n".join(f"      {name:<14} {value!r}" for name, value in pairs)


# -- what was left behind, said out loud -----------------------------------


def residue_lines(ticket_id: str, residue: dict[str, Any]) -> list[str]:
    """Every row in an audit table that this invocation has left behind.

    This replaces the deletes, and it is the whole reason the trade is
    acceptable: a row that is announced by id can be pruned by a human who
    has looked at it, whereas the rows this script used to remove were gone
    before anyone knew they had existed. Silence for weeks is what let that
    run — so this prints on the success path too, not only on failure.

    Pure, and returns lines rather than printing them, so the exact wording
    is assertable in `backend/tests/deploy/test_deep_check.py`.

    Note what is NOT here: a copy-pasteable statement for `runs` or
    `drafts`. Printing one would hand the next reader the precise text that
    destroyed runs 4-6, one paste away from a database, with none of the
    "is this row actually mine?" reasoning attached. The `SELECT` is
    printed; the decision to remove anything stays with the human.
    """
    if not residue.get("posted"):
        return ["cleanup     : nothing to undo — no webhook was sent."]

    lines = [
        "LEFT BEHIND (deliberately — this check never deletes from "
        f"{' or '.join(PROTECTED_TABLES)}):"
    ]

    run_id = residue.get("run_id")
    ambiguous = tuple(residue.get("ambiguous_run_ids") or ())
    if run_id is not None:
        lines.append(
            f"  runs row {run_id} on ticket {ticket_id} — the synthetic run this "
            "invocation caused, plus whatever drafts row the agent wrote for it."
        )
    if ambiguous:
        lines.append(
            f"  runs rows {list(ambiguous)} on ticket {ticket_id} — candidates this check "
            "could NOT attribute to itself. At least one belongs to something else, "
            "possibly a real customer. None of them was touched."
        )
    if run_id is None and not ambiguous:
        lines.append(
            f"  possibly a runs row on ticket {ticket_id}: a webhook WAS posted and no "
            "attributable run appeared before the deadline. If one lands later it stays."
        )

    ticket_run_ids = sorted(int(i) for i in residue.get("ticket_run_ids") or ())
    if ticket_run_ids:
        lines.append(
            f"  ticket {ticket_id} now carries {len(ticket_run_ids)} runs rows in the "
            f"feed: {ticket_run_ids}."
        )

    lines += [
        "  These rows COUNT toward GET /api/metrics — human_avoidance_rate, "
        "latency_p50_s, latency_p95_s and sample_count all include them. `runs` has no "
        "provenance column, so no reader can exclude them except by ticket_id, and real "
        "customer runs share this ticket. Repeated invocations therefore skew the numbers "
        "a human reads, VISIBLY, which is the trade this script now makes on purpose.",
        "  To prune: look at the row first, then remove it by hand if it is really yours.",
    ]
    ids_to_inspect = [i for i in ([run_id] if run_id is not None else []) + list(ambiguous)]
    if ids_to_inspect:
        inspect = ", ".join(str(i) for i in ids_to_inspect)
        lines.append(f"    SELECT * FROM runs WHERE id IN ({inspect});")
    return lines


# -- entrypoint ------------------------------------------------------------


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise PreconditionMissing(f"{name} is not set")
    return value


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="e.g. http://161.35.2.250:8080")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    base_url = args.base_url.rstrip("/")

    try:
        secret = _require("ZENDESK_WEBHOOK_SIGNING_SECRET")
        portal_token = _require("PORTAL_TOKEN")
        ticket_id = _require("CXFORGE_VERIFY_TICKET_ID")
    except PreconditionMissing as exc:
        print(f"[deep] PRECONDITION MISSING: {exc}", file=sys.stderr)
        return 2

    db_url = os.environ.get("CXFORGE_VERIFY_DB_URL", "").strip()
    comment_id = synthetic_comment_id()

    print(f"[deep] target      : {base_url}")
    print(f"[deep] ticket_id   : {ticket_id} (must be fetchable by the deployed agent)")
    print(f"[deep] comment_id  : {comment_id} (unique to this invocation)")

    # `residue` is filled in by `_check` as it goes and read by the `finally`
    # below. A FAILED deep check has still POSTed a webhook, so it has still
    # written a `tickets_seen` row and possibly a `runs` row — and the failure
    # path is the one that gets run over and over while somebody fixes the
    # deployment, so it is the one that most needs to say what it left.
    residue: dict[str, Any] = {
        "posted": False,
        "run_id": None,
        "ambiguous_run_ids": (),
        "ticket_run_ids": (),
    }
    try:
        _check(
            base_url, ticket_id, comment_id, secret, portal_token, args.timeout, db_url, residue
        )
    except AmbiguousRun as exc:
        # Ordered before the base class on purpose: the ids have to reach
        # `residue` before the `finally` reports, or the one failure mode where
        # a stranger's row may be involved is the one that names no rows.
        residue["ambiguous_run_ids"] = exc.candidate_run_ids
        print(f"[deep] FAIL: {exc}", file=sys.stderr)
        return 1
    except DeepCheckFailed as exc:
        print(f"[deep] FAIL: {exc}", file=sys.stderr)
        return 1
    finally:
        _report_residue(db_url, ticket_id, comment_id, residue)

    print("[deep] PASS — the core loop ran: signed webhook -> queue -> worker -> runs row.")
    return 0


def _check(
    base_url: str,
    ticket_id: str,
    comment_id: str,
    secret: str,
    portal_token: str,
    timeout: float,
    db_url: str,
    residue: dict[str, Any],
) -> None:
    with httpx.Client(follow_redirects=False) as client:
        _assert_gate_is_off(client, base_url, portal_token)

        baseline = _feed(client, base_url, portal_token)
        baseline_ids = {int(item["run_id"]) for item in baseline}
        prior_for_ticket = [i for i in baseline if i.get("ticket_id") == ticket_id]
        residue["ticket_run_ids"] = tuple(int(i["run_id"]) for i in prior_for_ticket)
        print(
            f"[deep] baseline    : {len(baseline)} runs in the feed, "
            f"{len(prior_for_ticket)} of them for this ticket "
            "(any of those would be a stale-row false pass; all are excluded)"
        )

        # Read-only corroboration, and the only provenance this schema has.
        # `runs` records nothing about what triggered a run, but `tickets_seen`
        # holds the originating `(ticket_id, comment_id)`. Snapshot it now so
        # that after the poll we can ask whether anything OTHER than this
        # invocation was accepted on this deliberately-reused ticket. Absent a
        # db_url this is simply unavailable, and the check says so rather than
        # pretending the single-candidate rule is stronger than it is.
        seen_baseline: set[str] | None = None
        if db_url:
            try:
                seen_baseline = _seen_comment_ids(db_url, ticket_id)
            except Exception as exc:  # noqa: BLE001 - any driver error is the same news
                print(
                    f"[deep] provenance  : UNAVAILABLE — could not read tickets_seen ({exc!r}). "
                    "Attribution falls back to the single-candidate rule alone."
                )
        else:
            print(
                "[deep] provenance  : UNAVAILABLE — CXFORGE_VERIFY_DB_URL is not set, so this "
                "check cannot tell whether a real comment also landed on this ticket during "
                "its window. Attribution rests on the single-candidate rule alone."
            )

        raw_body, headers = build_signed_request(
            secret=secret, ticket_id=ticket_id, comment_id=comment_id
        )
        started = time.monotonic()
        # Marked BEFORE the call, not after: a POST that times out may still
        # have been processed, so from the cleanup's point of view the row
        # might exist either way.
        residue["posted"] = True
        response = client.post(
            f"{base_url}{WEBHOOK_PATH}", content=raw_body, headers=headers, timeout=30.0
        )
        print(f"[deep] POST {WEBHOOK_PATH} -> {response.status_code} {response.text[:200]}")
        if response.status_code != 202:
            raise DeepCheckFailed(
                f"the signed webhook was answered with {response.status_code}, expected 202. "
                "401 means the signing secret here and on the deployment disagree; 500 means "
                "the deployment could not enqueue the job (ADR-017) or has no "
                "ZENDESK_WEBHOOK_SIGNING_SECRET at all."
            )
        if response.json().get("duplicate") is not False:
            raise DeepCheckFailed(
                "the deployment reported this event as a DUPLICATE, so it enqueued nothing. "
                f"comment_id {comment_id} should be unique — is something replaying it?"
            )

        print(f"[deep] polling {FEED_PATH} for a NEW runs row (deadline {timeout:.0f}s) ...")
        run = None
        while time.monotonic() - started < timeout:
            time.sleep(POLL_INTERVAL_SECONDS)
            feed = _feed(client, base_url, portal_token)
            residue["ticket_run_ids"] = tuple(
                int(i["run_id"]) for i in feed if i.get("ticket_id") == ticket_id
            )
            # An `AmbiguousRun` from here propagates to `main`, which records
            # its candidate ids into `residue` before the report runs. One of
            # them may be a customer's; a human needs the numbers, not just
            # the verdict.
            run = find_new_run(feed, ticket_id=ticket_id, baseline_run_ids=baseline_ids)
            if run is not None:
                break
        waited = time.monotonic() - started

        if run is None:
            raise DeepCheckFailed(
                f"no new runs row for ticket {ticket_id} after {waited:.1f}s. The webhook "
                "returned 202, so ingress accepted and (says it) enqueued the event — but "
                "nothing produced a run. That is the severed core loop: no worker consuming "
                "cxforge:jobs, no Redis behind the enqueue, or a worker that failed the run "
                "(check its ERROR log; ADR-003 releases the dedup row on failure)."
            )

        if seen_baseline is not None:
            _assert_nothing_else_arrived(db_url, ticket_id, comment_id, seen_baseline, run)

        residue["run_id"] = int(run["run_id"])
        interval = assert_row_is_real(run, ticket_id=ticket_id)
        print(f"[deep] runs row appeared after {waited:.1f}s")
        print(f"[deep]   {json.dumps(run, indent=2, sort_keys=True)}")
        print(
            f"[deep]   replied_at - received_at = {interval:.3f}s "
            f"(floor {MIN_PLAUSIBLE_RUN_SECONDS}s; the pre-ADR-004 defect measured 22µs)"
        )


def _assert_nothing_else_arrived(
    db_url: str,
    ticket_id: str,
    comment_id: str,
    seen_baseline: set[str],
    run: dict[str, Any],
) -> None:
    """Raise unless this invocation is the only thing that reached the ticket.

    The single-candidate rule in `find_new_run` closes the case where TWO
    runs rows appear. It cannot close the narrower one where a real customer
    comment lands on the reused ticket, produces the only new row, and this
    invocation's own run never writes one (ADR-003 releases the dedup row and
    returns on failure) — by elimination that stranger's row would look
    exactly like mine.

    `tickets_seen` closes it, because it is the one table that records what
    triggered a run. Any comment id on this ticket that was not in the
    pre-POST snapshot and is not the one this process minted means something
    else was accepted here during the window, and attribution is over.

    Deliberately conservative: a foreign comment whose run failed and wrote
    no row still fails this check. That is the correct side to err on — a
    refusal costs a re-run, and a false pass is what shipped a dead core loop
    for weeks.
    """
    try:
        seen_now = _seen_comment_ids(db_url, ticket_id)
    except Exception as exc:  # noqa: BLE001 - any driver error is the same news
        print(
            f"[deep] provenance  : UNAVAILABLE — could not re-read tickets_seen ({exc!r}). "
            "Attribution rests on the single-candidate rule alone."
        )
        return

    foreign = unexpected_comment_ids(seen_now, baseline=seen_baseline, ours=comment_id)
    if foreign:
        raise AmbiguousRun(
            f"runs row {run['run_id']} is the only new row for ticket {ticket_id}, but "
            f"tickets_seen shows {len(foreign)} other comment(s) accepted on that ticket "
            f"during this check's window: {sorted(foreign)}. So this row may not be mine — "
            "it may be the reply to one of those, with my own run having failed and released "
            "its own dedup row (ADR-003). `runs` records no provenance, so this cannot be "
            "settled from here. Refusing to report a pass on a row I cannot prove I caused. "
            "Nothing has been deleted.",
            (int(run["run_id"]),),
        )
    print(
        f"[deep] provenance  : corroborated — no comment other than {comment_id} was "
        f"accepted on ticket {ticket_id} during this check's window."
    )


def _report_residue(db_url: str, ticket_id: str, comment_id: str, residue: dict[str, Any]) -> None:
    """Say exactly what this invocation left in the database, then release the
    one row it can prove is its own.

    Ordering is deliberate: the residue report prints FIRST, before any
    statement runs, so a `ForbiddenStatement` or a dead connection cannot
    swallow the list of rows a human needs. The old version of this function
    printed nothing on the path that mattered and deleted three production
    audit rows on it.
    """
    for line in residue_lines(ticket_id, residue):
        print(f"[deep] {line}")

    if not residue["posted"]:
        return

    run_id = residue["run_id"]
    statements = dedup_cleanup_statements(ticket_id, comment_id)

    if not db_url:
        print(
            "[deep] DEDUP ROW NOT RELEASED — CXFORGE_VERIFY_DB_URL is not set, so this check "
            "cannot reach the database it just wrote to."
        )
        print(
            "[deep]   (It will NOT fall back to DATABASE_URL: in REMOTE mode that names "
            "the developer's own dev database, not the deployment's.)"
        )
        print("[deep]   Run this against the DEPLOYMENT's database:")
        for statement in statements:
            print(f"[deep]     {statement}")
        return

    if run_id is not None:
        print(f"[deep] raw runs row {run_id}, straight from the database (read-only):")
        print(_dump_run(db_url, run_id))
    print(f"[deep] dedup row   : {_execute(db_url, statements)}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
