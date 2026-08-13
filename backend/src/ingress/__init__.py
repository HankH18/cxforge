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

Ingress's job ends at "validated, deduped, accepted": starting the agent
run is T-5's job (LangGraph), deliberately never invoked from here.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import ValidationError

from data import get_connection

from .models import ZendeskWebhookPayload
from .signature import SignatureVerificationError, verify_signature

router = APIRouter(prefix="/webhooks", tags=["ingress"])


@router.post("/zendesk", status_code=202)
async def receive_zendesk_webhook(
    request: Request,
    x_zendesk_webhook_signature: str | None = Header(default=None),
    x_zendesk_webhook_signature_timestamp: str | None = Header(default=None),
) -> dict[str, object]:
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

    return {"status": "accepted", "duplicate": not is_new}
