"""Normalized models shared by every HelpdeskPort implementation.

Pinned verbatim in DESIGN §HelpdeskPort: ``Ticket``, ``Message`` (with its
``author_kind`` literal), and ``TicketStatus``. ``MessageRef`` and
``EscalationGroup`` are named by DESIGN but not shaped — kept minimal here.

Provider quirks never leak into these types. A field that only Zendesk (or
only email) can populate does not belong here; it belongs in that adapter's
own internals.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

TicketStatus = Literal["new", "open", "pending", "solved"]
AuthorKind = Literal["customer", "agent", "ai"]


class Ticket(BaseModel):
    """A normalized helpdesk ticket, as pinned in DESIGN §HelpdeskPort."""

    id: str
    subject: str
    requester_email: str
    status: TicketStatus
    tags: list[str]
    created_at: datetime


class TicketSummary(BaseModel):
    """One of the requester's *other* tickets, as seen by
    ``HelpdeskPort.fetch_requester_history`` (ADR-009 / BUILD-PLAN §1.5).

    Deliberately not a ``Ticket``. History is read to answer "has this person
    been here before, and about what" — the classifier needs the shape of the
    prior contact, not another copy of the requester's email on every row.
    Dropping ``requester_email`` also means a history list cannot become a
    second, unaudited source of identity: the run's requester comes from
    ``fetch_ticket``, once.

    Field list pinned in BUILD-PLAN §1.5: ``(id, subject, status, created_at,
    tags)``. Provider quirks stay in the adapter, as with every other model
    in this module.
    """

    id: str
    subject: str
    status: TicketStatus
    created_at: datetime
    tags: list[str]


class Message(BaseModel):
    """One conversation entry, as pinned in DESIGN §HelpdeskPort.

    ``author_kind`` distinguishes the requester, a human agent, and the
    dedicated AI agent user — mapping provider identity to this literal is
    entirely the adapter's job (see ``ZendeskAdapter``); callers never see a
    raw provider user id.
    """

    id: str
    author_kind: AuthorKind
    text: str
    public: bool
    created_at: datetime


class MessageRef(BaseModel):
    """Reference to a comment a ``post_public_reply``/``post_internal_note``
    call created. Deliberately minimal — just enough for a caller (or a
    test) to confirm which ticket got which kind of message, without
    exposing any provider-specific comment shape."""

    ticket_id: str
    message_id: str
    public: bool


class EscalationGroup(BaseModel):
    """The target group for ``assign_group``.

    SPEC R6 names exactly one group ("the escalation group") for this
    system, so this stays a plain reference rather than an enum of many
    teams: whoever calls ``assign_group`` (T-6's escalation engine) owns
    deciding which group that is, via its own config. ``group_id`` is the
    provider's own identifier for it (Zendesk's numeric group id, carried as
    a string so this type stays provider-agnostic); ``name`` is a
    human-readable label for traces/portal display only — never sent to the
    provider.
    """

    group_id: str
    name: str
