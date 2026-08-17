"""Pinned Zendesk webhook payload (DESIGN §Webhook ingress).

DESIGN pins exactly `{ticket_id, comment_id, requester_email, subject,
latest_comment_text}` — no author field. T-4's acceptance also requires
"events authored by the AI user are dropped", so this model adds exactly
ONE field beyond the pinned five: `comment_author_id`.

That field is NOT invented independently of the runbook: docs/zendesk-
runbook.md's trigger JSON body includes it via the `{{current_user.id}}`
placeholder, under this exact name, and instructs the human to add it for
that reason. `ingress/__init__.py` compares it against `ZENDESK_AI_USER_ID`
to implement the self-event drop. If this field is ever renamed, the
runbook's JSON body must be updated in the same change or self-event drop
silently breaks for anyone following the runbook.

It is optional (defaults to `None`) rather than required: a payload that
omits it still validates as a well-formed ingress event (self-event drop
simply can't fire, which is a safe default — the tag-based nullifier in the
trigger is loop-guard line one and does not depend on this field at all).

**Why the two id fields are constrained non-empty (2026-08-17).** They used
to be plain `str`, which accepts `""` — and Zendesk renders an unresolvable
placeholder as the empty string rather than failing. Measured on the live
account: the trigger's original `{{ticket.latest_comment_id}}` placeholder
does not exist, so every delivery arrived with `"comment_id": ""`. Because
ingress dedupes on `(ticket_id, comment_id)`, all of a ticket's comments
collapsed onto the single key `(N, "")`: the first was processed and **every
customer follow-up after it was silently discarded as a duplicate**, with a
`202 {"duplicate": true}` and, at the time, not one log line saying a real
message had been thrown away.

An empty id is therefore not a tolerable input, it is a poisoned primary
key. Rejecting it is deliberately the loud answer: a 400 shows up
immediately in Zendesk's own webhook activity log and in ingress's ERROR
stream, whereas accepting it degrades into "customers get one answer and
then silence", which is exactly the failure that survived for weeks.

The alternative considered and rejected was synthesising a fallback key
(e.g. from `X-Zendesk-Webhook-Invocation-Id`). It keeps events flowing, but
it changes what `tickets_seen.comment_id` *means* and it makes two
invocations describing the same comment dedupe as two distinct events — so
it would trade silent drops for duplicate customer replies. Given the
trigger placeholder is now correct (`{{ticket.latest_comment.id}}`), this
constraint is a tripwire that should never fire in normal operation, not a
workload.
"""

from __future__ import annotations

from pydantic import BaseModel, field_validator

_EMPTY_COMMENT_ID_MESSAGE = (
    "comment_id is empty, so this event cannot be deduplicated. Ingress keys "
    "idempotency on (ticket_id, comment_id), so an empty comment_id collapses "
    "every comment on a ticket onto one key and silently discards every "
    "follow-up as a duplicate. The cause is a Zendesk trigger whose comment-id "
    "placeholder does not resolve: use {{ticket.latest_comment.id}}, NOT "
    "{{ticket.latest_comment_id}} (which renders as the empty string). See "
    "docs/zendesk-runbook.md step 7."
)

_EMPTY_TICKET_ID_MESSAGE = (
    "ticket_id is empty, so this event cannot be attributed to a ticket or "
    "deduplicated. Check the Zendesk trigger's JSON body renders "
    "{{ticket.id}} (docs/zendesk-runbook.md step 7)."
)


class ZendeskWebhookPayload(BaseModel):
    """The pinned webhook body, plus the documented author placeholder."""

    ticket_id: str
    comment_id: str
    requester_email: str
    subject: str
    latest_comment_text: str
    comment_author_id: str | None = None

    # Two validators rather than one over both fields: each half of the
    # primary key gets the diagnosis that actually applies to it, and the
    # operator reading a 400 body is told which placeholder to go fix.
    #
    # Whitespace-only counts as blank, but a valid value is returned
    # unmodified — these ids ARE the `tickets_seen` primary key, and quietly
    # rewriting one would make the stored key differ from what Zendesk sent.

    @field_validator("comment_id")
    @classmethod
    def _comment_id_must_identify_a_comment(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(_EMPTY_COMMENT_ID_MESSAGE)
        return value

    @field_validator("ticket_id")
    @classmethod
    def _ticket_id_must_identify_a_ticket(cls, value: str) -> str:
        if not value.strip():
            raise ValueError(_EMPTY_TICKET_ID_MESSAGE)
        return value
