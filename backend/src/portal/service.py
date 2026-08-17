"""Portal business logic: feed/draft/gate/metrics queries against the
``runs``/``drafts``/``settings`` tables T-1 already created, plus R13's
metric math.

``routes.py`` is a thin FastAPI shim over this module — every function here
is plain Python (no ``Request``/``HTTPException``), so it can be unit-tested
directly against the real docker-compose Postgres without going through
``TestClient`` at all, exactly like ``agent.store``'s own functions.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

# ``agent.config.GATE_SETTING_KEY`` / ``agent.store.read_gate_enabled`` are
# T-5's pinned read path for the gate (see those modules' docstrings: "T-8
# owns *writing* it"). Importing them here — rather than re-deriving the
# settings key or the OFF-default/true-values parsing independently — is
# what GUARANTEES the round trip: ``write_gate`` below writes literally the
# same ``settings.key`` ``agent.store.read_gate_enabled`` reads, so there is
# no second place this could drift out of sync. These are read-only imports
# of another ticket's pinned contract, not an edit to agent/**.
from agent.config import GATE_SETTING_KEY
from agent.store import read_gate_enabled
from data import get_connection
from helpdesk.port import HelpdeskPort
from portal.errors import DraftNotFound, DraftNotPending
from portal.schemas import DraftResponse, DraftStatus, FeedItem, MetricsResponse

_DEFAULT_LANGFUSE_HOST = "https://cloud.langfuse.com"

# DESIGN R10/R13 pin "escalation reason" (feed field) and
# "escalations_by_reason" (metric). The specific reason(s)
# (`agent.escalation_seam.EscalationTrigger.reason` — billing/human_request/
# unknown_case/out_of_procedure/low_confidence/frustration/complexity) are
# persisted on `runs.reasons` (`backend/src/data/schema.py`, a `text[]`
# column — see that module's docstring for the migration that added it to
# an already-existing database), written by `agent.store.record_run`'s
# `reasons` parameter, threaded there from the escalation decision by
# `agent.nodes.act`. A run can carry more than one reason (DESIGN's
# `EscalationCall.reasons: list[Reason]`, and `escalation.engine`'s hard
# rules can independently co-fire with the classifier) — `_escalation_reason`
# below and `compute_metrics`'s `escalations_by_reason` both account for
# that; see each function's own docstring for exactly how.
_TERMINAL_OUTCOMES = ("auto_sent", "gated_sent", "escalated")


def read_gate() -> bool:
    return read_gate_enabled()


def write_gate(enabled: bool) -> None:
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO settings (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (GATE_SETTING_KEY, "true" if enabled else "false"),
        )


# -- feed (R10) ---------------------------------------------------------


def _trace_url(trace_id: str | None) -> str | None:
    """DESIGN's ``trace_url`` field — a link to this run's Langfuse trace.

    The URL shape is unchanged from T-8 (BUILD-PLAN §1.6 freezes it), but
    what it points at changed under W2-C1: ``agent.nodes.act`` now reports
    the ``trace_id`` it mints to Langfuse (``agent.llm.emit_trace``), so
    these links resolve instead of 404ing. Before W2-C1 the id was a bare
    ``uuid.uuid4().hex`` that was never told to anyone.

    ``{host}/trace/{id}`` is Langfuse's own project-agnostic redirect, which
    is why this can build a working link without knowing the project id.
    Measured 2026-08-16 against a trace this code emitted:
    ``GET https://us.cloud.langfuse.com/trace/<id>`` → **307** to
    ``/project/<project_id>/traces/<id>`` → **200**.

    ``LANGFUSE_HOST`` is the variable the app reads — not
    ``LANGFUSE_BASE_URL`` (`docs/DESIGN.md`) — and its default below is the
    EU region while the ``cxforge`` project is on ``us.cloud.langfuse.com``,
    so an unset ``LANGFUSE_HOST`` builds a syntactically fine link into the
    wrong region rather than failing. That is a deployment concern, checked
    by ``backend/tests/deploy/test_env_forwarding.py``, not something this
    function can detect.
    """
    if not trace_id:
        return None
    host = os.environ.get("LANGFUSE_HOST", _DEFAULT_LANGFUSE_HOST).rstrip("/")
    return f"{host}/trace/{trace_id}"


def _escalation_reason(outcome: str | None, reasons: list[str] | None) -> str | None:
    """``FeedItem.escalation_reason`` — a single display string, so a run
    escalated for more than one reason (see module docstring) reports every
    one of them, comma-joined, rather than picking just one. ``None`` for a
    non-escalated run, and — defensively, though it should never happen via
    the real graph, since ``agent.nodes.act`` always threads the full
    decision's reasons — for an escalated run with no reasons recorded,
    rather than reintroducing a fake placeholder string."""
    if outcome != "escalated" or not reasons:
        return None
    return ", ".join(reasons)


_FEED_COLUMNS = (
    "r.id, r.ticket_id, r.route, r.confidence, r.outcome, r.trace_id, "
    "r.received_at, r.replied_at, r.reasons, d.id, d.status, d.body, d.edited_body"
)


def _row_to_feed_item(row: tuple[Any, ...]) -> FeedItem:
    (
        run_id,
        ticket_id,
        route,
        confidence,
        outcome,
        trace_id,
        received_at,
        replied_at,
        reasons,
        draft_id,
        draft_status,
        body,
        edited_body,
    ) = row
    sent_body = None
    if draft_status in ("approved", "auto_sent"):
        sent_body = edited_body if edited_body is not None else body
    return FeedItem(
        run_id=run_id,
        ticket_id=ticket_id,
        route=route,
        confidence=confidence,
        outcome=outcome,
        draft_id=draft_id,
        draft_status=draft_status,
        draft_body=body,
        edited_body=edited_body,
        sent_body=sent_body,
        escalation_reason=_escalation_reason(outcome, reasons),
        trace_url=_trace_url(trace_id),
        received_at=received_at,
        replied_at=replied_at,
    )


def fetch_feed(status: DraftStatus | None) -> list[FeedItem]:
    """``GET /api/feed?status=`` — every run left-joined to its draft
    (``record_draft`` always creates exactly one draft per run — see
    ``agent.store``'s docstring — so this is 1:1 in practice; ``LEFT JOIN``
    rather than ``INNER`` only so a row is never silently dropped if that
    invariant is ever violated). ``status`` filters on ``drafts.status``
    (the schema's own literal column name for a draft's review state) when
    given."""
    sql = f"SELECT {_FEED_COLUMNS} FROM runs r LEFT JOIN drafts d ON d.run_id = r.id"
    params: tuple[Any, ...] = ()
    if status is not None:
        sql += " WHERE d.status = %s"
        params = (status,)
    sql += " ORDER BY r.received_at DESC NULLS LAST, r.id DESC"
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [_row_to_feed_item(row) for row in rows]


# -- draft edit/approve/reject (R11/R12) ---------------------------------


def _fetch_draft_row(cur: Any, draft_id: int) -> tuple[int, int, str | None, str | None, str, str]:
    """Locks the draft row (``FOR UPDATE OF d``) for the remainder of the
    caller's transaction — approve/reject/edit all read-check-then-write
    the same row, and the lock is what makes two concurrent approves of the
    same draft resolve to one send and one 409, never two sends."""
    cur.execute(
        """
        SELECT d.id, d.run_id, d.body, d.edited_body, d.status, r.ticket_id
        FROM drafts d
        JOIN runs r ON r.id = d.run_id
        WHERE d.id = %s
        FOR UPDATE OF d
        """,
        (draft_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise DraftNotFound(draft_id)
    return row


def edit_draft(draft_id: int, new_body: str) -> DraftResponse:
    """``PUT /api/drafts/{id}`` — stores ``edited_body``, leaving ``body``
    (the original composed draft) intact for audit. Only valid while the
    draft is still ``pending``: once sent/approved/rejected there is
    nothing left to edit."""
    with get_connection() as conn, conn.cursor() as cur:
        d_id, run_id, body, _old_edited, status, ticket_id = _fetch_draft_row(cur, draft_id)
        if status != "pending":
            raise DraftNotPending(draft_id, status)
        cur.execute("UPDATE drafts SET edited_body = %s WHERE id = %s", (new_body, draft_id))
    return DraftResponse(
        draft_id=d_id,
        run_id=run_id,
        ticket_id=ticket_id,
        status="pending",
        body=body or "",
        edited_body=new_body,
        sent_body=None,
    )


def approve_draft(draft_id: int, port: HelpdeskPort) -> DraftResponse:
    """``POST /api/drafts/{id}/approve`` — sends the EDITED body when one
    exists, else the original (DESIGN's whole point of edit-then-approve),
    via the injected ``HelpdeskPort``; records the run's outcome as
    ``gated_sent`` (R12: gated/approved sends are human-touched, excluded
    from the human-avoidance numerator).

    Raises ``DraftNotPending`` — never sends — if the draft isn't
    ``pending`` (already approved, already rejected, or was an autonomous
    send to begin with). The port call happens INSIDE the same
    ``get_connection()`` transaction as the row lock and the two UPDATEs:
    if ``port.post_public_reply`` raises, ``data.get_connection`` rolls the
    whole transaction back (see its own docstring) and the draft is left
    exactly ``pending`` — so a failed send is retryable via a second
    ``approve`` call, never a state where the port succeeded but the DB
    disagrees, and never a partial DB write with no send.
    """
    with get_connection() as conn, conn.cursor() as cur:
        d_id, run_id, body, edited_body, status, ticket_id = _fetch_draft_row(cur, draft_id)
        if status != "pending":
            raise DraftNotPending(draft_id, status)
        send_body = edited_body if edited_body is not None else (body or "")
        port.post_public_reply(ticket_id, send_body)
        replied_at = datetime.now(UTC)
        cur.execute("UPDATE drafts SET status = 'approved' WHERE id = %s", (draft_id,))
        cur.execute(
            "UPDATE runs SET outcome = 'gated_sent', replied_at = %s WHERE id = %s",
            (replied_at, run_id),
        )
    return DraftResponse(
        draft_id=d_id,
        run_id=run_id,
        ticket_id=ticket_id,
        status="approved",
        body=body or "",
        edited_body=edited_body,
        sent_body=send_body,
    )


def reject_draft(draft_id: int) -> DraftResponse:
    """``POST /api/drafts/{id}/reject`` — sends nothing; marks the draft
    ``rejected`` and the run's outcome ``rejected``. Same
    already-decided guard as ``approve_draft``."""
    with get_connection() as conn, conn.cursor() as cur:
        d_id, run_id, body, edited_body, status, ticket_id = _fetch_draft_row(cur, draft_id)
        if status != "pending":
            raise DraftNotPending(draft_id, status)
        cur.execute("UPDATE drafts SET status = 'rejected' WHERE id = %s", (draft_id,))
        cur.execute("UPDATE runs SET outcome = 'rejected' WHERE id = %s", (run_id,))
    return DraftResponse(
        draft_id=d_id,
        run_id=run_id,
        ticket_id=ticket_id,
        status="rejected",
        body=body or "",
        edited_body=edited_body,
        sent_body=None,
    )


# -- metrics (R13) --------------------------------------------------------


def _percentile(values: list[float], pct: float) -> float | None:
    """Linear-interpolation percentile — the same method
    ``numpy.percentile``'s default ("linear") uses — over a sorted sample.
    DESIGN pins the p50/p95 metric names, not a specific percentile
    algorithm; this one is standard and easy to hand-verify, and is applied
    identically to both p50 and p95 so the two stay comparable.

    ``None`` over an empty sample, not ``0.0`` (W2-C3). ``numpy.percentile``
    raises on an empty array; returning zero was this function's own
    invention, and it is the one wrong answer with consequences — see
    ``portal.schemas.MetricsResponse``."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    frac = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * frac


def compute_metrics() -> MetricsResponse:
    """``GET /api/metrics`` — DESIGN §Metric definitions, pinned verbatim:

        human_avoidance_rate = tickets solved via auto-sent replies only /
        all tickets reaching a terminal handling (solved or escalated).
        Gated approved sends and escalations count against it.

    Concretely, over ``runs.outcome``:
      - numerator: ``auto_sent`` only.
      - denominator: ``auto_sent`` + ``gated_sent`` + ``escalated`` — the
        three outcomes that represent a terminal handling (solved
        autonomously, solved via a gated/human-touched send, or escalated).
        ``rejected`` (draft killed, nothing sent — not solved, not
        escalated) and ``off_topic`` (redirected, ticket left ``open`` —
        not solved, not escalated) and pending/undecided runs
        (``outcome IS NULL``) are excluded from both: none of them is a
        "terminal handling (solved or escalated)" by DESIGN's own wording.
      - a ``gated_sent`` run therefore counts in the denominator (it did
        reach a terminal, solved handling) but NEVER in the numerator (R12:
        "Gated ... sends SHALL be recorded as human-touched and excluded
        from the human-avoidance numerator") — this is the exact
        distinction the rate would collapse if gated sends were folded
        into the numerator, which DESIGN explicitly forbids doing.

    ``latency_p50_s``/``latency_p95_s``: DESIGN — "latency = webhook receipt
    -> public reply posted, autonomous mode only" — computed only over
    ``outcome = 'auto_sent'`` rows, in seconds, from ``received_at`` to
    ``replied_at``.

    That quotation used to be **false about the code beneath it**
    (`docs/STATE.md §4.1`): ``received_at`` was minted inside ``agent.nodes.
    act``, the last node of the graph, so the interval excluded every model
    call and measured only the HelpdeskPort calls. W1-A5 / ADR-004 fixed the
    measurement rather than the sentence — receipt time is now stamped in
    the ingress handler and carried on the job payload — so the definition
    is left standing because it is now true. Checked clause by clause on
    2026-08-16 against `agent.nodes.act` and the query below; nothing in
    this paragraph is a claim about intent.

    ``sample_count``, and ``None`` percentiles over an empty sample: W2-C3.
    ``len(latencies)`` is the size of THIS sample — the ``auto_sent`` rows
    with both timestamps — which is why it is taken from the list the
    percentiles are computed from rather than counted separately. A
    ``gated_sent`` run has a latency and is deliberately not in it.

    ``escalations_by_reason``: SPEC R13's "escalation counts by reason",
    grouped over ``runs.reasons`` (the array ``agent.store.record_run``
    persists — see that function's and ``data.schema``'s docstrings) for
    every ``outcome = 'escalated'`` row. A run that escalated for more than
    one reason (DESIGN's ``EscalationCall.reasons: list[Reason]`` is
    genuinely multi-valued, and ``escalation.engine``'s hard rules can
    independently co-fire alongside it) is counted under EVERY one of its
    reasons, not just one — so ``sum(escalations_by_reason.values())`` can
    legitimately exceed the number of escalated runs. That is correct, not
    a double-counting bug: each bucket answers "how many escalated runs
    cited this reason", and a run citing two reasons genuinely belongs in
    both buckets. ``SELECT ... FROM runs, unnest(reasons) AS reason`` below
    is exactly that — one output row per (run, reason) pair, so ``GROUP BY
    reason`` counts each run once per reason it carries.
    """
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT outcome, count(*) FROM runs WHERE outcome IS NOT NULL GROUP BY outcome"
        )
        outcome_counts: dict[str, int] = dict(cur.fetchall())

        cur.execute(
            "SELECT EXTRACT(EPOCH FROM (replied_at - received_at)) FROM runs "
            "WHERE outcome = 'auto_sent' AND replied_at IS NOT NULL AND received_at IS NOT NULL"
        )
        latencies = [float(row[0]) for row in cur.fetchall() if row[0] is not None]

        cur.execute(
            "SELECT reason, count(*) FROM runs, unnest(reasons) AS reason "
            "WHERE outcome = 'escalated' GROUP BY reason"
        )
        escalations_by_reason: dict[str, int] = {
            reason: int(count) for reason, count in cur.fetchall()
        }

    auto_sent = outcome_counts.get("auto_sent", 0)
    denominator = sum(outcome_counts.get(o, 0) for o in _TERMINAL_OUTCOMES)
    rate = (auto_sent / denominator) if denominator > 0 else 0.0

    return MetricsResponse(
        human_avoidance_rate=rate,
        latency_p50_s=_percentile(latencies, 0.5),
        latency_p95_s=_percentile(latencies, 0.95),
        sample_count=len(latencies),
        escalations_by_reason=escalations_by_reason,
    )
