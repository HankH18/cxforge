"""The HelpdeskPort boundary — DESIGN §HelpdeskPort, pinned verbatim.

T-5/T-6/T-8 depend on this Protocol, not on any concrete adapter. Do not add,
rename, or reshape a method here without going back to the human: this is
the T-2/T-3 boundary the contract suite is written against exactly once.

``fetch_requester_history`` is the one method added since that pinning, and
it went through exactly that route: it is the owner's decision **ADR-009**,
with the signature frozen in ``docs/BUILD-PLAN.md §1.5`` before either
adapter was touched. It exists because customer history is the only PRD line
item that appeared in neither the code nor SPEC's non-goals — a repeat
complainer should read differently from a first-time asker. Both adapters
implement it and the parametrized contract suite covers it for both, which
is what keeps R14 (the port boundary is real, not a Zendesk wrapper) true.
"""

from __future__ import annotations

from typing import Protocol

from helpdesk.models import (
    EscalationGroup,
    Message,
    MessageRef,
    Ticket,
    TicketStatus,
    TicketSummary,
)


class HelpdeskPort(Protocol):
    def fetch_ticket(self, ticket_id: str) -> Ticket: ...

    def fetch_conversation(self, ticket_id: str) -> list[Message]: ...

    def fetch_requester_history(
        self, requester_email: str, *, exclude_ticket_id: str, limit: int = 5
    ) -> list[TicketSummary]: ...

    def post_public_reply(self, ticket_id: str, html_body: str) -> MessageRef: ...

    def post_internal_note(self, ticket_id: str, body: str) -> MessageRef: ...

    def add_tags(self, ticket_id: str, tags: list[str]) -> None: ...

    def set_status(self, ticket_id: str, status: TicketStatus) -> None: ...

    def assign_group(self, ticket_id: str, group: EscalationGroup) -> None: ...
