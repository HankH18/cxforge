"""EmailAdapter — a stub HelpdeskPort over an in-memory fake mail transport.

T-3 (SPEC R14): proves the port boundary is real. This adapter shares
nothing with ZendeskAdapter's transport (no HTTP, no OAuth, no Zendesk
ticket JSON) yet must pass the identical HelpdeskPort contract suite —
see ``backend/tests/contract/test_port_contract.py``.

Domain model — a ticket IS a mail thread:

- a public reply is an outbound email to the requester, recorded by
  ``InMemoryEmailTransport`` (never actually sent — no SMTP/IMAP, no
  sockets, no network calls; see README's Portability section for exactly
  what a production email channel would add on top of this stub);
- an internal note is a non-public annotation on the thread. Plain email
  has no "private comment" concept, so a production adapter would need a
  side channel (its own store, keyed by thread) to hold one outside the
  mail protocol entirely — this stub's local thread store stands in for
  that side channel. A note is therefore recorded on the thread but never
  handed to ``InMemoryEmailTransport``;
- tags, status, and group assignment are local thread metadata with no
  wire representation in SMTP/IMAP. A production adapter would persist
  them the same way (its own store), never by encoding them into the
  email itself.

This module also owns a small test/dev seeding surface (``seed_ticket``,
``seed_comment``, ``group_id_for``) that stands in for what a production
adapter would learn from *inbound* mail (IMAP polling or a webhook — see
README). Those methods are not part of ``HelpdeskPort``; only the contract
suite's harness (``backend/tests/contract/_fake_email.py``) calls them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import TYPE_CHECKING

from helpdesk.errors import HelpdeskAPIError
from helpdesk.models import (
    AuthorKind,
    EscalationGroup,
    Message,
    MessageRef,
    Ticket,
    TicketStatus,
    TicketSummary,
)
from helpdesk.port import HelpdeskPort

# Loop-guard tag convention (DESIGN §HelpdeskPort / T-4 runbook). Every
# write funnels through _touch(), exactly as ZendeskAdapter funnels every
# write through _update_ticket — forgetting the tag on one write path is
# structurally impossible rather than a per-method discipline problem.
AI_PROCESSED_TAG = "ai-processed"

# Placeholder identities the stub uses to tell customer/agent/ai apart. A
# production adapter would derive these from real headers (the inbound
# message's From:, a configured human-agent roster, the AI mailbox's own
# address) instead of fixed constants — see README's Portability section.
AI_AGENT_ADDRESS = "support-ai@othram-support.example"
HUMAN_AGENT_ADDRESS = "agent@othram-support.example"
DEFAULT_REQUESTER_EMAIL = "requester@example.com"
DEFAULT_SUBJECT = "Support request"

# Deterministic, monotonically increasing timestamps (an incrementing
# counter over a fixed epoch) rather than wall-clock time, so seeded
# messages sort correctly regardless of how fast a test seeds them.
_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass
class SentEmail:
    """One outbound message ``InMemoryEmailTransport`` recorded — what a
    real SMTP send would have produced. Kept only for inspection; nothing
    in the shared contract suite reads this (it has no Protocol-level
    equivalent), but adapter-specific tests can (see
    ``test_email_adapter.py``)."""

    ticket_id: str
    to: str
    subject: str
    html_body: str
    message_id: str


class InMemoryEmailTransport:
    """Fake mail transport: records every outbound send in memory instead
    of opening an SMTP connection. No sockets, no network calls — the stub
    stays a stub (T-3 non-goal)."""

    def __init__(self) -> None:
        self.sent: list[SentEmail] = []
        self._ids = count(1)

    def send(self, *, ticket_id: str, to: str, subject: str, html_body: str) -> str:
        message_id = f"<reply-{next(self._ids)}@stub.local>"
        self.sent.append(
            SentEmail(
                ticket_id=ticket_id,
                to=to,
                subject=subject,
                html_body=html_body,
                message_id=message_id,
            )
        )
        return message_id


@dataclass
class _ThreadMessage:
    """One entry in a thread's local message log. ``from_address`` (not an
    ``AuthorKind``) is stored here — ``author_kind`` is derived from it on
    read via ``_author_kind_for``, so the mapping is a real function of
    identity, the same shape ``ZendeskAdapter._author_kind`` maps a raw
    Zendesk user id to the same literal, rather than a value just carried
    through unchanged."""

    id: str
    from_address: str
    text: str
    public: bool
    created_at: datetime


@dataclass
class _Thread:
    """A mail thread — the email domain's ticket. Tags/status/group are
    metadata this stub keeps locally, exactly as a production adapter
    would (email has no wire format for any of them)."""

    id: str
    subject: str
    requester_email: str
    status: TicketStatus
    tags: list[str]
    created_at: datetime
    group_id: str | None = None
    group_name: str | None = None
    messages: list[_ThreadMessage] = field(default_factory=list)


class EmailAdapter:
    """HelpdeskPort implementation over an in-memory fake mail transport."""

    def __init__(self, *, transport: InMemoryEmailTransport | None = None) -> None:
        self.transport = transport or InMemoryEmailTransport()
        self._threads: dict[str, _Thread] = {}
        self._thread_ids = count(1)
        self._message_ids = count(1)
        self._clock = count(0)

    # -- HelpdeskPort ---------------------------------------------------

    def fetch_ticket(self, ticket_id: str) -> Ticket:
        thread = self._get_thread(ticket_id)
        return Ticket(
            id=thread.id,
            subject=thread.subject,
            requester_email=thread.requester_email,
            status=thread.status,
            tags=list(thread.tags),
            created_at=thread.created_at,
        )

    def fetch_conversation(self, ticket_id: str) -> list[Message]:
        thread = self._get_thread(ticket_id)
        messages = [
            Message(
                id=entry.id,
                author_kind=self._author_kind_for(thread, entry.from_address),
                text=entry.text,
                public=entry.public,
                created_at=entry.created_at,
            )
            for entry in thread.messages
        ]
        # Mirrors ZendeskAdapter.fetch_conversation: sort explicitly rather
        # than trusting store/insertion order to already be chronological.
        messages.sort(key=lambda message: message.created_at)
        return messages

    def fetch_requester_history(
        self, requester_email: str, *, exclude_ticket_id: str, limit: int = 5
    ) -> list[TicketSummary]:
        """Prior threads from the same address, newest first (ADR-009).

        In the email domain "the requester's other tickets" is "the other
        threads whose ``From:`` is this address" — no search API, no query
        language, just the local thread store this stub keeps in place of
        an IMAP mailbox. The ordering and the ``exclude_ticket_id`` /
        ``limit`` semantics are the Protocol's, not email's, so they are
        implemented here rather than left to whatever order the store
        happens to iterate in.
        """
        threads = [
            thread
            for thread in self._threads.values()
            if thread.requester_email == requester_email and thread.id != exclude_ticket_id
        ]
        threads.sort(key=lambda thread: thread.created_at, reverse=True)
        return [
            TicketSummary(
                id=thread.id,
                subject=thread.subject,
                status=thread.status,
                created_at=thread.created_at,
                tags=list(thread.tags),
            )
            for thread in threads[:limit]
        ]

    def post_public_reply(self, ticket_id: str, html_body: str) -> MessageRef:
        thread = self._get_thread(ticket_id)
        message_id = self.transport.send(
            ticket_id=ticket_id,
            to=thread.requester_email,
            subject=f"Re: {thread.subject}",
            html_body=html_body,
        )
        self._record_message(
            thread,
            message_id=message_id,
            from_address=AI_AGENT_ADDRESS,
            text=html_body,
            public=True,
        )
        self._touch(thread)
        return MessageRef(ticket_id=ticket_id, message_id=message_id, public=True)

    def post_internal_note(self, ticket_id: str, body: str) -> MessageRef:
        # Deliberately never calls self.transport.send: plain email has no
        # private-comment concept, so an internal note must never be
        # emailed to anyone.
        thread = self._get_thread(ticket_id)
        message_id = f"<note-{next(self._message_ids)}@stub.local>"
        self._record_message(
            thread, message_id=message_id, from_address=AI_AGENT_ADDRESS, text=body, public=False
        )
        self._touch(thread)
        return MessageRef(ticket_id=ticket_id, message_id=message_id, public=False)

    def add_tags(self, ticket_id: str, tags: list[str]) -> None:
        thread = self._get_thread(ticket_id)
        for tag in tags:
            if tag not in thread.tags:
                thread.tags.append(tag)
        self._touch(thread)

    def set_status(self, ticket_id: str, status: TicketStatus) -> None:
        thread = self._get_thread(ticket_id)
        thread.status = status
        self._touch(thread)

    def assign_group(self, ticket_id: str, group: EscalationGroup) -> None:
        thread = self._get_thread(ticket_id)
        thread.group_id = group.group_id
        thread.group_name = group.name
        self._touch(thread)

    # -- test/dev seeding surface (NOT part of HelpdeskPort) -------------
    # Stands in for what a production adapter would learn from inbound
    # mail (IMAP poll or webhook — see README). Only the contract suite's
    # harness (backend/tests/contract/_fake_email.py) calls these.

    def seed_ticket(
        self,
        *,
        requester_email: str = DEFAULT_REQUESTER_EMAIL,
        subject: str = DEFAULT_SUBJECT,
        status: TicketStatus = "open",
        tags: list[str] | None = None,
    ) -> str:
        ticket_id = str(next(self._thread_ids))
        self._threads[ticket_id] = _Thread(
            id=ticket_id,
            subject=subject,
            requester_email=requester_email,
            status=status,
            tags=list(tags or []),
            created_at=self._next_timestamp(),
        )
        return ticket_id

    def seed_comment(
        self,
        ticket_id: str,
        *,
        author: AuthorKind,
        text: str,
        public: bool = True,
        created_at: str | None = None,
    ) -> None:
        thread = self._get_thread(ticket_id)
        message_id = f"<seed-{next(self._message_ids)}@stub.local>"
        self._record_message(
            thread,
            message_id=message_id,
            from_address=self._address_for_author_kind(thread, author),
            text=text,
            public=public,
            created_at=created_at,
        )

    def group_id_for(self, ticket_id: str) -> str | None:
        return self._get_thread(ticket_id).group_id

    # -- internals --------------------------------------------------------

    def _get_thread(self, ticket_id: str) -> _Thread:
        thread = self._threads.get(ticket_id)
        if thread is None:
            raise HelpdeskAPIError(404, f"no such mail thread: {ticket_id}")
        return thread

    def _record_message(
        self,
        thread: _Thread,
        *,
        message_id: str,
        from_address: str,
        text: str,
        public: bool,
        created_at: str | None = None,
    ) -> None:
        timestamp = datetime.fromisoformat(created_at) if created_at else self._next_timestamp()
        thread.messages.append(
            _ThreadMessage(
                id=message_id,
                from_address=from_address,
                text=text,
                public=public,
                created_at=timestamp,
            )
        )

    def _touch(self, thread: _Thread) -> None:
        """The ONLY path every write above funnels through — unconditionally
        folds the ai-processed loop-guard tag into local thread metadata,
        mirroring ``ZendeskAdapter._update_ticket``. There is no way to
        construct a write that skips this."""
        if AI_PROCESSED_TAG not in thread.tags:
            thread.tags.append(AI_PROCESSED_TAG)

    def _address_for_author_kind(self, thread: _Thread, author: AuthorKind) -> str:
        if author == "customer":
            return thread.requester_email
        if author == "ai":
            return AI_AGENT_ADDRESS
        return HUMAN_AGENT_ADDRESS

    def _author_kind_for(self, thread: _Thread, from_address: str) -> AuthorKind:
        if from_address == AI_AGENT_ADDRESS:
            return "ai"
        if from_address == thread.requester_email:
            return "customer"
        return "agent"

    def _next_timestamp(self) -> datetime:
        return _EPOCH + timedelta(seconds=next(self._clock))


if TYPE_CHECKING:

    def _typed_conformance_check() -> None:
        """Never executed — exists only for mypy to evaluate. HelpdeskPort
        is a runtime-unchecked ``Protocol`` (no ABC, no ``@runtime_checkable``
        registration), so nothing else in this codebase forces a check that
        EmailAdapter actually implements every method the Protocol declares,
        with matching signatures. This assignment only type-checks if it
        does — a drift here becomes a real ``mypy`` failure instead of
        something we merely assumed."""
        _port: HelpdeskPort = EmailAdapter()
