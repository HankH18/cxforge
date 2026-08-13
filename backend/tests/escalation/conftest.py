"""Shared fixtures and builders for the escalation suite.

No Postgres, no real LLM, no ``SKIP_DB_TESTS`` gating needed: every test
here is a pure/unit test over ``backend/src/escalation/**`` and the
``agent.nodes.decide``/``agent.nodes.act`` wiring, using
``backend/tests/escalation/fakes.py``'s doubles and (only where a test
reaches ``agent.store``) ``monkeypatch`` rather than a live database
connection. This mirrors CI's own constraint: T-0's GitHub Actions workflow
runs ``pytest -m 'not live'`` with no db service (``SKIP_DB_TESTS=1``), so
this suite (unmarked, therefore always collected) must not require one.
"""

from __future__ import annotations

from datetime import UTC, datetime

from data import Case
from helpdesk.models import AuthorKind, Message, Ticket

DEFAULT_TICKET_ID = "T-42"
DEFAULT_REQUESTER_EMAIL = "requester@example.com"


def make_ticket(**overrides: object) -> Ticket:
    defaults: dict[str, object] = dict(
        id=DEFAULT_TICKET_ID,
        subject="Test ticket",
        requester_email=DEFAULT_REQUESTER_EMAIL,
        status="open",
        tags=[],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    defaults.update(overrides)
    return Ticket(**defaults)  # type: ignore[arg-type]


def make_message(
    text: str, *, author_kind: AuthorKind = "customer", message_id: str = "m1"
) -> Message:
    return Message(
        id=message_id,
        author_kind=author_kind,
        text=text,
        public=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def make_conversation(text: str) -> list[Message]:
    """The minimal shape ``agent.nodes``' escalation helpers expect: one
    public customer message."""
    return [make_message(text)]


def make_case(**overrides: object) -> Case:
    defaults: dict[str, object] = dict(
        case_id="MFG-2025-0001",
        requester_email=DEFAULT_REQUESTER_EMAIL,
        requester_name="Jane Requester",
        stage="extraction",
        stage_entered_at=datetime(2026, 1, 1).date(),
        last_updated=datetime(2026, 1, 5).date(),
        eta_weeks=3,
        dna_profile_available=False,
        photos_available=True,
    )
    defaults.update(overrides)
    return Case(**defaults)  # type: ignore[arg-type]
