"""HelpdeskPort contract suite (DESIGN §HelpdeskPort).

Written ONCE against the Protocol and parametrized over every adapter via
the `adapter_harness` fixture (see conftest.py). Nothing here may import a
concrete adapter or reach into its transport — anything an adapter needs
beyond the Protocol itself belongs on `AdapterHarness`, not in a test body,
so this file stays exactly as valid once T-3's EmailAdapter joins the
parametrization as it is with Zendesk alone.

Where a write's effect isn't observable through the Protocol's read methods
(`assign_group`'s target group isn't a `Ticket` field — DESIGN pins that
field list), the harness exposes one narrow read-back method
(`group_id_for`) instead of a mock-inspection shortcut.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from helpdesk.models import EscalationGroup
from helpdesk.port import HelpdeskPort

from .conftest import AdapterHarness

pytestmark = pytest.mark.contract


def test_fetch_ticket_returns_normalized_ticket(adapter_harness: AdapterHarness) -> None:
    seeded = adapter_harness.seed_ticket()

    ticket = adapter_harness.port.fetch_ticket(seeded.ticket_id)

    assert ticket.id == seeded.ticket_id
    assert ticket.requester_email == seeded.requester_email
    assert ticket.status in ("new", "open", "pending", "solved")
    assert isinstance(ticket.tags, list)
    assert ticket.created_at is not None
    assert ticket.subject


def test_fetch_conversation_maps_author_kinds_including_ai(adapter_harness: AdapterHarness) -> None:
    seeded = adapter_harness.seed_ticket()
    adapter_harness.seed_comment(
        seeded.ticket_id, author="customer", text="Where's my case?", public=True
    )
    adapter_harness.seed_comment(
        seeded.ticket_id, author="agent", text="Let me check on that.", public=False
    )
    adapter_harness.seed_comment(
        seeded.ticket_id, author="ai", text="Your case is in extraction.", public=True
    )

    conversation = adapter_harness.port.fetch_conversation(seeded.ticket_id)

    assert [message.author_kind for message in conversation] == ["customer", "agent", "ai"]
    assert conversation[2].text == "Your case is in extraction."


def test_fetch_conversation_sorts_out_of_order_messages_chronologically(
    adapter_harness: AdapterHarness,
) -> None:
    seeded = adapter_harness.seed_ticket()
    # Seed deliberately out of chronological order so this can only pass if
    # the adapter sorts by created_at rather than trusting insertion/API
    # order.
    adapter_harness.seed_comment(
        seeded.ticket_id,
        author="customer",
        text="first",
        created_at="2026-01-01T00:00:00+00:00",
    )
    adapter_harness.seed_comment(
        seeded.ticket_id,
        author="ai",
        text="third",
        created_at="2026-01-01T00:00:02+00:00",
    )
    adapter_harness.seed_comment(
        seeded.ticket_id,
        author="agent",
        text="second",
        created_at="2026-01-01T00:00:01+00:00",
    )

    conversation = adapter_harness.port.fetch_conversation(seeded.ticket_id)

    assert [message.text for message in conversation] == ["first", "second", "third"]
    assert [message.created_at for message in conversation] == sorted(
        message.created_at for message in conversation
    )


def test_public_reply_and_internal_note_are_separate_and_correctly_scoped(
    adapter_harness: AdapterHarness,
) -> None:
    seeded = adapter_harness.seed_ticket()

    reply_ref = adapter_harness.port.post_public_reply(seeded.ticket_id, "<p>All set.</p>")
    note_ref = adapter_harness.port.post_internal_note(seeded.ticket_id, "Escalating per policy.")

    assert reply_ref.public is True
    assert note_ref.public is False
    assert reply_ref.message_id != note_ref.message_id

    conversation = adapter_harness.port.fetch_conversation(seeded.ticket_id)
    public_messages = [message for message in conversation if message.public]
    private_messages = [message for message in conversation if not message.public]
    # Two distinct messages, not one merged/overwritten comment — this is
    # what would break if a write path tried to combine both into a single
    # update (Zendesk allows only one comment per PUT).
    assert len(public_messages) == 1
    assert len(private_messages) == 1
    assert "All set" in public_messages[0].text
    assert "Escalating" in private_messages[0].text


WriteOp = Callable[[HelpdeskPort, str], object]

_WRITE_OPS: list[tuple[str, WriteOp]] = [
    ("post_public_reply", lambda port, ticket_id: port.post_public_reply(ticket_id, "<p>hi</p>")),
    ("post_internal_note", lambda port, ticket_id: port.post_internal_note(ticket_id, "note")),
    ("add_tags", lambda port, ticket_id: port.add_tags(ticket_id, ["vip"])),
    ("set_status", lambda port, ticket_id: port.set_status(ticket_id, "pending")),
    (
        "assign_group",
        lambda port, ticket_id: port.assign_group(
            ticket_id, EscalationGroup(group_id="42", name="Escalations")
        ),
    ),
]


@pytest.mark.parametrize(
    "write_op", [op for _, op in _WRITE_OPS], ids=[name for name, _ in _WRITE_OPS]
)
def test_every_write_appends_ai_processed_tag(
    adapter_harness: AdapterHarness, write_op: WriteOp
) -> None:
    """The loop-guard tag (SPEC/DESIGN webhook nullifier) must land on every
    single write path individually — a per-operation check, not one check
    after an arbitrary write, since a regression could plausibly add the tag
    on some paths and miss others."""
    seeded = adapter_harness.seed_ticket()

    write_op(adapter_harness.port, seeded.ticket_id)

    ticket = adapter_harness.port.fetch_ticket(seeded.ticket_id)
    assert "ai-processed" in ticket.tags


def test_add_tags_is_additive(adapter_harness: AdapterHarness) -> None:
    seeded = adapter_harness.seed_ticket(tags=["existing-tag"])

    adapter_harness.port.add_tags(seeded.ticket_id, ["vip"])
    adapter_harness.port.add_tags(seeded.ticket_id, ["urgent"])

    ticket = adapter_harness.port.fetch_ticket(seeded.ticket_id)
    assert set(ticket.tags) >= {"existing-tag", "vip", "urgent", "ai-processed"}


def test_set_status_sends_normalized_value(adapter_harness: AdapterHarness) -> None:
    seeded = adapter_harness.seed_ticket()

    adapter_harness.port.set_status(seeded.ticket_id, "solved")

    assert adapter_harness.port.fetch_ticket(seeded.ticket_id).status == "solved"


def test_assign_group_sends_normalized_value(adapter_harness: AdapterHarness) -> None:
    seeded = adapter_harness.seed_ticket()
    group = EscalationGroup(group_id="77", name="Escalations")

    adapter_harness.port.assign_group(seeded.ticket_id, group)

    assert adapter_harness.group_id_for(seeded.ticket_id) == "77"
