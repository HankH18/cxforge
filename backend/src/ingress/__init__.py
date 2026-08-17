"""Webhook ingress: receipt, HMAC verification, idempotency, self-event drop.

`POST /webhooks/zendesk` (DESIGN §Webhook ingress, SPEC R1). Each step is a
hard gate on the next:

1. HMAC-SHA256 verify the RAW request body (`ingress.signature`) before
   anything else touches it. Missing/invalid signature -> 401, and neither
   the body nor the database is touched.
2. Parse + validate the pinned payload (`ingress.models`). A malformed body
   -> 400, never a 500.
3. Self-event drop: if `comment_author_id` matches `ZENDESK_AI_USER_ID`,
   accept-and-noop without writing to `tickets_seen`. This is loop-guard
   line two; line one is the Zendesk trigger's `tags not include
   ai-processed` condition (docs/zendesk-runbook.md) — this endpoint is a
   second line of defense, not the primary guard.
4. Idempotency: `INSERT ... ON CONFLICT DO NOTHING` into `tickets_seen`
   keyed on `(ticket_id, comment_id)`, checked via `cur.rowcount`. The
   uniqueness is enforced by the table's primary key, so two concurrent
   requests for the same event can never both "win" — there is no
   read-then-write race window.
5. Dispatch: enqueue a `worker.jobs.TicketJob` onto Redis (`cxforge:jobs`,
   arq task `run_ticket`) — but only when step 4 actually inserted a row,
   so a duplicate delivery enqueues nothing. The job carries `received_at`,
   stamped at the top of this handler, which is what makes `runs.received_at`
   mean "webhook receipt" rather than "just before the Zendesk write"
   (ADR-004). If the enqueue itself fails the endpoint returns **500**, not
   202 (ADR-017) — see the comment at that call site for why the two
   failure modes answer differently.

Ingress's job ends at "validated, deduped, dispatched". It still does not
*run* the agent inline: holding Zendesk's connection open through 20–60s of
model calls would serialize all intake and time out the webhook. The run
happens in the `worker` container (ADR-002).

**Historical note, deliberately kept.** Until 2026-08-16 this docstring said
"starting the agent run is T-5's job (LangGraph), deliberately never invoked
from here" — and `portal/deps.py` said it was T-10's scenario runner's job,
and T-5's scope could not reach ingress. The result was that nothing in
`backend/src` ever called `run_agent` at all: the core loop was severed for
the entire life of the project while 702 tests stayed green
(`docs/STATE.md §2`). The wiring below is that omission's fix; the note is
here so nobody re-derives the "deliberate" reading from the code's shape.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import ValidationError

from data import get_connection
from worker.jobs import TicketJob
from worker.queue import JobQueue, get_job_queue

from .models import ZendeskWebhookPayload
from .signature import SignatureVerificationError, verify_signature

router = APIRouter(prefix="/webhooks", tags=["ingress"])

logger = logging.getLogger(__name__)

# ADR-017 gives Zendesk the retry by answering 500 — but **Zendesk deactivates
# a webhook after sustained consecutive failures**, so a long Redis outage
# could disable the endpoint entirely, which is worse than losing one comment.
# These two attempts absorb the overwhelmingly likely case (a transient blip
# against this request's own short-lived pool) so it never reaches Zendesk as a
# 5xx at all. The 500 remains the honest answer when the broker is genuinely
# down.
#
# Deliberately constants, not settings: the values only matter as a bound, and
# a knob nobody tunes is worse than a number with a reason next to it. Keep the
# total well under a second — this delay is spent holding Zendesk's connection
# open, so "improving" it into a long exponential backoff would trade a rare
# lost comment for a routinely timed-out webhook.
ENQUEUE_ATTEMPTS = 2
ENQUEUE_RETRY_DELAY_SECONDS = 0.2

# Module-level so the `Depends(...)` call is not evaluated in a function
# default (ruff B008) — same pattern as `portal/routes.py::_PORT`.
_QUEUE = Depends(get_job_queue)


def _release_dedup_row(ticket_id: str, comment_id: str) -> None:
    """Undo step 4's insert. Shares ADR-003's rationale with
    `worker.main.release_dedup_row`: a `(ticket_id, comment_id)` marked seen
    but never dispatched is dead forever, because `tickets_seen` carries no
    state beyond the two ids."""
    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM tickets_seen WHERE ticket_id = %s AND comment_id = %s",
            (ticket_id, comment_id),
        )


@router.post("/zendesk", status_code=202)
async def receive_zendesk_webhook(
    request: Request,
    x_zendesk_webhook_signature: str | None = Header(default=None),
    x_zendesk_webhook_signature_timestamp: str | None = Header(default=None),
    queue: JobQueue = _QUEUE,
) -> dict[str, object]:
    # True webhook-receipt time (ADR-004), taken before anything else in the
    # handler so it is not quietly shifted by signature verification or the
    # database round-trip. This value rides on the job payload all the way
    # into `agent.nodes.act`, where it becomes `runs.received_at`.
    received_at = datetime.now(UTC)

    # Read the exact bytes Zendesk sent BEFORE any JSON parsing happens —
    # signing (below) and payload validation (further below) both operate
    # on this same raw buffer, never on a re-serialized form of it.
    raw_body = await request.body()

    secret = os.environ.get("ZENDESK_WEBHOOK_SIGNING_SECRET")
    if not secret:
        # Server misconfiguration, not a client error — but never proceed
        # as if a request were verified when it wasn't.
        raise HTTPException(
            status_code=500, detail="ZENDESK_WEBHOOK_SIGNING_SECRET is not configured"
        )
    try:
        verify_signature(
            secret=secret,
            timestamp=x_zendesk_webhook_signature_timestamp,
            signature=x_zendesk_webhook_signature,
            raw_body=raw_body,
        )
    except SignatureVerificationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    try:
        payload = ZendeskWebhookPayload.model_validate_json(raw_body)
    except ValidationError as exc:
        # str(exc), not exc.errors(): a "the body isn't JSON at all" error
        # embeds the raw input (here, bytes) in its error dict, which isn't
        # JSON-serializable and would turn this 400 into a 500.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    ai_user_id = os.environ.get("ZENDESK_AI_USER_ID")
    if ai_user_id and payload.comment_author_id == ai_user_id:
        return {"status": "dropped", "reason": "self-authored", "duplicate": False}

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tickets_seen (ticket_id, comment_id) VALUES (%s, %s) "
            "ON CONFLICT (ticket_id, comment_id) DO NOTHING",
            (payload.ticket_id, payload.comment_id),
        )
        is_new = cur.rowcount == 1

    if is_new:
        # AFTER the insert and ONLY when it inserted (DESIGN §1.1). Ordered
        # this way so the dedup row always exists before a consumer can pick
        # the job up: the reverse order lets a fast worker start a run for a
        # ticket that ingress has not yet claimed.
        #
        # Built ONCE, outside the retry loop below. A retry must never re-stamp
        # `received_at` — the whole point of ADR-004 is that the value is
        # webhook-receipt time, and moving it forward on attempt 2 would quietly
        # shorten every latency measurement by however long the broker took to
        # fail.
        job = TicketJob(
            ticket_id=payload.ticket_id,
            comment_id=payload.comment_id,
            received_at=received_at,
        )
        try:
            await _enqueue_with_retry(queue, job)
        except asyncio.CancelledError:
            # `CancelledError` is a BaseException, so the `except Exception`
            # inside `_enqueue_with_retry` does NOT catch it — the same
            # asymmetry `worker.main.run_ticket` guards against, on the ingress
            # side. uvicorn cancels this handler task on client disconnect and
            # on graceful shutdown, and the window covers both the enqueue await
            # and the retry sleep between attempts.
            #
            # Uncaught, the sequence is: row inserted, never enqueued, never
            # released, no response. Zendesk retries, hits the surviving row,
            # gets `duplicate: true` and a 202 — and nothing ever runs. That is
            # the dead-comment state ADR-003 and ADR-017 both exist to prevent,
            # reached from the ingress side.
            #
            # Nothing is awaited between this `except` and the `raise`, so a
            # second cancellation cannot land mid-release.
            try:
                _release_dedup_row(payload.ticket_id, payload.comment_id)
            except Exception:  # pragma: no cover - only when Postgres is also down
                logger.exception(
                    "could not release tickets_seen row for ticket %s (comment %s) "
                    "after a cancelled enqueue",
                    payload.ticket_id,
                    payload.comment_id,
                )
            logger.error(
                "enqueue cancelled for ticket %s (comment %s) — client disconnect "
                "or shutdown; the dedup row was released so Zendesk can retry",
                payload.ticket_id,
                payload.comment_id,
            )
            raise
        except Exception as exc:
            # ADR-017: a failed *dispatch* is not a failed *run*. The work was
            # never accepted at all, and Zendesk does not retry a 202 — so
            # answering 202 here would silently drop the customer's event,
            # leaving an ERROR log nobody is watching as the only trace. 500
            # hands the retry back to Zendesk, the only party that still has
            # the event.
            #
            # Release the dedup row first, or Zendesk's own retry arrives and
            # is swallowed as a duplicate — the retry we just asked for would
            # be defeated by the idempotency guard.
            #
            # (A failed *run*, once dispatch succeeded, still returns 202 and
            # still releases the row — ADR-002/003, `worker.main.run_ticket`.)
            try:
                _release_dedup_row(payload.ticket_id, payload.comment_id)
            except Exception:  # pragma: no cover - only when Postgres is also down
                logger.exception(
                    "could not release tickets_seen row for ticket %s (comment %s) "
                    "after a failed enqueue; Zendesk's retry will be seen as a duplicate",
                    payload.ticket_id,
                    payload.comment_id,
                )
            logger.error(
                "could not enqueue run_ticket for ticket %s (comment %s) after "
                "%d attempts: %r",
                payload.ticket_id,
                payload.comment_id,
                ENQUEUE_ATTEMPTS,
                exc,
                exc_info=True,
            )
            # `detail` is a fixed string. `exc` is deliberately NOT interpolated
            # into it: a broker error carries the Redis DSN, which in a
            # misconfigured deployment carries a password. The exception is on
            # the operator's ERROR log, where it belongs, and never in a body
            # returned over the public webhook endpoint.
            raise HTTPException(
                status_code=500, detail="could not dispatch the event for processing"
            ) from exc

    return {"status": "accepted", "duplicate": not is_new}


async def _enqueue_with_retry(queue: JobQueue, job: TicketJob) -> None:
    """Publish `job`, retrying once on failure (ADR-017).

    Raises the last exception if every attempt fails; the caller turns that into
    the 500. `CancelledError` is never caught here — it belongs to the caller's
    handler, which owns the dedup row.
    """
    for attempt in range(1, ENQUEUE_ATTEMPTS + 1):
        try:
            await queue.enqueue(job)
            return
        except Exception as exc:
            if attempt >= ENQUEUE_ATTEMPTS:
                raise
            # A transient blip — most likely this request's own short-lived
            # pool losing a connection. WARNING, not ERROR: ADR-003 makes the
            # ERROR stream load-bearing (it is the only signal a run failed),
            # so a blip that recovers must not pollute it.
            logger.warning(
                "enqueue attempt %d/%d failed for ticket %s (comment %s), "
                "retrying in %.2fs: %r",
                attempt,
                ENQUEUE_ATTEMPTS,
                job.ticket_id,
                job.comment_id,
                ENQUEUE_RETRY_DELAY_SECONDS,
                exc,
            )
            # `asyncio.sleep`, never `time.sleep`: this is an async handler on
            # the event loop, and blocking it would stall every other in-flight
            # webhook for the duration. Cancellation can land here rather than
            # in `enqueue`; it propagates to the caller's `CancelledError`
            # handler, which owns the dedup row.
            await asyncio.sleep(ENQUEUE_RETRY_DELAY_SECONDS)
