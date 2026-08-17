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

---------------------------------------------------------------------------
Reading the effect back, and the database this must NOT use
---------------------------------------------------------------------------
The row is read back through `GET /api/feed` on the deployment under test.
That is on purpose: it is the deployed application's own view of its own
database, over the same public entry point a human uses, and it works in
REMOTE mode where the droplet's Postgres publishes no host port at all.

Cleanup (and a corroborating raw-row dump) additionally needs SQL, and that
comes from `CXFORGE_VERIFY_DB_URL` — a **separate variable that is never
defaulted from `DATABASE_URL`**. In REMOTE mode `.env`'s `DATABASE_URL` is
`postgresql://…@localhost:5432/othram`: the *developer's dev database*, not
the droplet's. Silently falling back to it would point the cleanup DELETEs at
a completely different database from the one the run happened in — the exact
shape of every bug in `docs/STATE.md §6`. When it is absent this check says
so loudly and prints the exact SQL to run, and never pretends it tidied up.

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
    """
    candidates = [
        item
        for item in feed
        if item.get("ticket_id") == ticket_id and item.get("run_id") not in baseline_run_ids
    ]
    if not candidates:
        return None
    # Newest first, so a (pathological) double-run reports the later row.
    return max(candidates, key=lambda item: int(item["run_id"]))


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


# -- cleanup ---------------------------------------------------------------
#
# Everything below deletes by the ids THIS invocation created and nothing
# else. `DELETE FROM runs WHERE ticket_id = …` would be shorter and would
# also delete a run somebody else's invocation is still looking at, on a
# ticket that is deliberately reused. The dedup row is keyed on
# `(ticket_id, comment_id)` and the comment_id is unique per invocation, so
# that one is already precise.


def cleanup_statements(ticket_id: str, comment_id: str, run_id: int | None) -> list[str]:
    """The exact SQL that undoes this invocation, as text.

    Shared by the two cleanup paths on purpose: what actually runs when
    `CXFORGE_VERIFY_DB_URL` is set, and what is *printed* for a human to run
    when it is not. One source, so the printed remediation can never drift
    from the statements that were tested.
    """
    statements = [
        f"DELETE FROM tickets_seen WHERE ticket_id = '{ticket_id}' "
        f"AND comment_id = '{comment_id}';"
    ]
    if run_id is not None:
        statements.insert(0, f"DELETE FROM drafts WHERE run_id = {run_id};")
        statements.insert(1, f"DELETE FROM runs WHERE id = {run_id};")
    return statements


def _execute(db_url: str, statements: list[str]) -> str:
    import psycopg

    counts = []
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        for statement in statements:
            cur.execute(statement.encode())
            counts.append(f"{cur.rowcount}")
    return ", ".join(f"{s.split()[2]}={c}" for s, c in zip(statements, counts, strict=True))


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
    # written a `tickets_seen` row and possibly a `runs` row: tidying only on
    # success would make the failure path — the one that gets run over and
    # over while somebody fixes the deployment — the one that accumulates
    # droppings.
    residue: dict[str, Any] = {"posted": False, "run_id": None}
    try:
        _check(base_url, ticket_id, comment_id, secret, portal_token, args.timeout, residue)
    except DeepCheckFailed as exc:
        print(f"[deep] FAIL: {exc}", file=sys.stderr)
        return 1
    finally:
        _tidy(db_url, ticket_id, comment_id, residue)

    print("[deep] PASS — the core loop ran: signed webhook -> queue -> worker -> runs row.")
    return 0


def _check(
    base_url: str,
    ticket_id: str,
    comment_id: str,
    secret: str,
    portal_token: str,
    timeout: float,
    residue: dict[str, Any],
) -> None:
    with httpx.Client(follow_redirects=False) as client:
        _assert_gate_is_off(client, base_url, portal_token)

        baseline = _feed(client, base_url, portal_token)
        baseline_ids = {int(item["run_id"]) for item in baseline}
        prior_for_ticket = [i for i in baseline if i.get("ticket_id") == ticket_id]
        print(
            f"[deep] baseline    : {len(baseline)} runs in the feed, "
            f"{len(prior_for_ticket)} of them for this ticket "
            "(any of those would be a stale-row false pass; all are excluded)"
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
            run = find_new_run(
                _feed(client, base_url, portal_token),
                ticket_id=ticket_id,
                baseline_run_ids=baseline_ids,
            )
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

        residue["run_id"] = int(run["run_id"])
        interval = assert_row_is_real(run, ticket_id=ticket_id)
        print(f"[deep] runs row appeared after {waited:.1f}s")
        print(f"[deep]   {json.dumps(run, indent=2, sort_keys=True)}")
        print(
            f"[deep]   replied_at - received_at = {interval:.3f}s "
            f"(floor {MIN_PLAUSIBLE_RUN_SECONDS}s; the pre-ADR-004 defect measured 22µs)"
        )


def _tidy(db_url: str, ticket_id: str, comment_id: str, residue: dict[str, Any]) -> None:
    """Undo this invocation's writes, or say plainly that it could not."""
    if not residue["posted"]:
        print("[deep] cleanup     : nothing to undo — no webhook was sent.")
        return

    run_id = residue["run_id"]
    statements = cleanup_statements(ticket_id, comment_id, run_id)

    if not db_url:
        print(
            "[deep] CLEANUP INCOMPLETE — CXFORGE_VERIFY_DB_URL is not set, so this check "
            "cannot reach the database it just wrote to and has left its rows behind."
        )
        print(
            "[deep]   (It will NOT fall back to DATABASE_URL: in REMOTE mode that names "
            "the developer's own dev database, not the deployment's.)"
        )
        print("[deep]   Run these against the DEPLOYMENT's database:")
        for statement in statements:
            print(f"[deep]     {statement}")
        return

    if run_id is not None:
        print(f"[deep] raw runs row {run_id}, straight from the database, before cleanup:")
        print(_dump_run(db_url, run_id))
    print(f"[deep] cleanup     : {_execute(db_url, statements)}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
