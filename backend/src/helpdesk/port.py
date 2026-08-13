"""The HelpdeskPort boundary — DESIGN §HelpdeskPort, pinned verbatim.

T-5/T-6/T-8 depend on this Protocol, not on any concrete adapter. Do not add,
rename, or reshape a method here without going back to the human: this is
the T-2/T-3 boundary the contract suite is written against exactly once.
"""

from __future__ import annotations

from typing import Protocol

from helpdesk.models import EscalationGroup, Message, MessageRef, Ticket, TicketStatus


class HelpdeskPort(Protocol):
    def fetch_ticket(self, ticket_id: str) -> Ticket: ...

    def fetch_conversation(self, ticket_id: str) -> list[Message]: ...

    def post_public_reply(self, ticket_id: str, html_body: str) -> MessageRef: ...

    def post_internal_note(self, ticket_id: str, body: str) -> MessageRef: ...

    def add_tags(self, ticket_id: str, tags: list[str]) -> None: ...

    def set_status(self, ticket_id: str, status: TicketStatus) -> None: ...

    def assign_group(self, ticket_id: str, group: EscalationGroup) -> None: ...
