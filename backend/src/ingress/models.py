"""Pinned Zendesk webhook payload (DESIGN §Webhook ingress).

DESIGN pins exactly `{ticket_id, comment_id, requester_email, subject,
latest_comment_text}` — no author field. T-4's acceptance also requires
"events authored by the AI user are dropped", so this model adds exactly
ONE field beyond the pinned five: `comment_author_id`.

That field is NOT invented independently of the runbook: docs/zendesk-
runbook.md's trigger JSON body includes it via the `{{comment.author.id}}`
placeholder, under this exact name, and instructs the human to add it for
that reason. `ingress/__init__.py` compares it against `ZENDESK_AI_USER_ID`
to implement the self-event drop. If this field is ever renamed, the
runbook's JSON body must be updated in the same change or self-event drop
silently breaks for anyone following the runbook.

It is optional (defaults to `None`) rather than required: a payload that
omits it still validates as a well-formed ingress event (self-event drop
simply can't fire, which is a safe default — the tag-based nullifier in the
trigger is loop-guard line one and does not depend on this field at all).
"""

from __future__ import annotations

from pydantic import BaseModel


class ZendeskWebhookPayload(BaseModel):
    """The pinned webhook body, plus the documented author placeholder."""

    ticket_id: str
    comment_id: str
    requester_email: str
    subject: str
    latest_comment_text: str
    comment_author_id: str | None = None
