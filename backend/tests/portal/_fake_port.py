"""In-memory ``HelpdeskPort`` recorder for the portal test suite.

Not a test module itself (no ``test_`` prefix — pytest never collects it),
mirroring ``backend/tests/contract/_fake_zendesk.py``'s convention.

Deliberately independent of ``helpdesk.email_adapter.EmailAdapter`` (T-5's
graph suite's own fake, ``backend/tests/graph/fakes.py``): that adapter
round-trips ``fetch_ticket``/``fetch_conversation`` through a seeded thread
store, which the fixture-based tests in this suite (``test_drafts.py``,
``test_metrics.py``) have no need of — they build ``runs``/``drafts`` rows
directly through ``agent.store``, never call ``fetch_*``, and only care
about counting/asserting exactly which port calls ``portal.service`` made.
``test_gate_integration.py`` (the one true end-to-end test that runs the
real LangGraph pipeline) uses ``EmailAdapter`` instead, for exactly the
``fetch_*`` round-tripping this recorder skips.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count
from typing import Any

from helpdesk.errors import HelpdeskAPIError
from helpdesk.models import EscalationGroup, Message, MessageRef, Ticket, TicketStatus


@dataclass
class FakeHelpdeskPort:
    """Records every call instead of making one. ``fail_next_reply`` lets a
    test simulate a transient upstream failure on the very next
    ``post_public_reply`` (and only that one) to prove ``approve`` leaves
    the draft ``pending`` — retryable — rather than a state the DB and the
    provider disagree about."""

    public_replies: list[tuple[str, str]] = field(default_factory=list)
    internal_notes: list[tuple[str, str]] = field(default_factory=list)
    tags_added: list[tuple[str, list[str]]] = field(default_factory=list)
    statuses_set: list[tuple[str, str]] = field(default_factory=list)
    groups_assigned: list[tuple[str, EscalationGroup]] = field(default_factory=list)
    fail_next_reply: bool = False
    _message_ids: Any = field(default_factory=lambda: count(1))

    def fetch_ticket(self, ticket_id: str) -> Ticket:  # pragma: no cover - unused by portal
        raise NotImplementedError("portal never calls fetch_ticket")

    def fetch_conversation(self, ticket_id: str) -> list[Message]:  # pragma: no cover
        raise NotImplementedError("portal never calls fetch_conversation")

    def post_public_reply(self, ticket_id: str, html_body: str) -> MessageRef:
        if self.fail_next_reply:
            self.fail_next_reply = False
            raise HelpdeskAPIError(503, "simulated transient failure")
        self.public_replies.append((ticket_id, html_body))
        message_id = f"fake-reply-{next(self._message_ids)}"
        return MessageRef(ticket_id=ticket_id, message_id=message_id, public=True)

    def post_internal_note(self, ticket_id: str, body: str) -> MessageRef:
        self.internal_notes.append((ticket_id, body))
        message_id = f"fake-note-{next(self._message_ids)}"
        return MessageRef(ticket_id=ticket_id, message_id=message_id, public=False)

    def add_tags(self, ticket_id: str, tags: list[str]) -> None:
        self.tags_added.append((ticket_id, list(tags)))

    def set_status(self, ticket_id: str, status: TicketStatus) -> None:
        self.statuses_set.append((ticket_id, status))

    def assign_group(self, ticket_id: str, group: EscalationGroup) -> None:
        self.groups_assigned.append((ticket_id, group))
