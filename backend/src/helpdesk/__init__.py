"""Helpdesk port and its adapters.

T-2 defines the HelpdeskPort Protocol, its normalized models, and the full
ZendeskAdapter implementation. T-3 adds the EmailAdapter stub that proves
the port is swappable.
"""

from __future__ import annotations

from helpdesk.errors import (
    HelpdeskAPIError,
    HelpdeskConfigError,
    HelpdeskError,
    RateLimited,
    ServerUnavailable,
)
from helpdesk.models import EscalationGroup, Message, MessageRef, Ticket, TicketStatus
from helpdesk.port import HelpdeskPort
from helpdesk.zendesk_adapter import ZendeskAdapter

__all__ = [
    "EscalationGroup",
    "HelpdeskAPIError",
    "HelpdeskConfigError",
    "HelpdeskError",
    "HelpdeskPort",
    "Message",
    "MessageRef",
    "RateLimited",
    "ServerUnavailable",
    "Ticket",
    "TicketStatus",
    "ZendeskAdapter",
]
