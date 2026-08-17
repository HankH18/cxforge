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


# -- fetch_requester_history (ADR-009 / BUILD-PLAN §1.5) ---------------------
#
# The one method added to the Protocol since DESIGN pinned it. It is the
# strongest test of R14 in this file, because the two adapters answer it in
# genuinely different ways — Zendesk has to build a Search API query
# (`type:ticket requester:<email>`, sorted by parameter, current ticket
# filtered client-side) while EmailAdapter walks a local thread store — and
# every assertion below is written against the Protocol's semantics, so
# neither implementation can satisfy them by accident.

OTHER_REQUESTER = "someone-else@example.com"


def test_requester_history_returns_prior_tickets_newest_first(
    adapter_harness: AdapterHarness,
) -> None:
    """Ordering is the Protocol's, not the provider's. A history read for a
    classifier is only useful if the most recent contact is first — an
    adapter that returned insertion order, or whatever its store iterates
    in, fails here."""
    oldest = adapter_harness.seed_ticket(subject="first contact")
    middle = adapter_harness.seed_ticket(subject="second contact")
    current = adapter_harness.seed_ticket(subject="today's question")

    history = adapter_harness.port.fetch_requester_history(
        current.requester_email, exclude_ticket_id=current.ticket_id
    )

    assert [summary.id for summary in history] == [middle.ticket_id, oldest.ticket_id]
    assert [summary.subject for summary in history] == ["second contact", "first contact"]


def test_requester_history_excludes_the_ticket_being_processed(
    adapter_harness: AdapterHarness,
) -> None:
    """The current ticket is the requester's too, and a provider search
    returns it. Leaving it in would feed the classifier the message it is
    already reading, labelled as prior history."""
    adapter_harness.seed_ticket(subject="a previous one")
    current = adapter_harness.seed_ticket(subject="today's question")

    history = adapter_harness.port.fetch_requester_history(
        current.requester_email, exclude_ticket_id=current.ticket_id
    )

    assert current.ticket_id not in [summary.id for summary in history]
    assert [summary.subject for summary in history] == ["a previous one"]


def test_requester_history_never_returns_another_requesters_tickets(
    adapter_harness: AdapterHarness,
) -> None:
    """The property with real consequences: a history leak would put one
    customer's subject lines into another customer's classification context,
    and from there into a trace and a support agent's screen."""
    adapter_harness.seed_ticket(subject="not yours", requester_email=OTHER_REQUESTER)
    mine = adapter_harness.seed_ticket(subject="mine, earlier")
    current = adapter_harness.seed_ticket(subject="today's question")

    history = adapter_harness.port.fetch_requester_history(
        current.requester_email, exclude_ticket_id=current.ticket_id
    )

    assert [summary.id for summary in history] == [mine.ticket_id]


def test_requester_history_honours_the_limit(adapter_harness: AdapterHarness) -> None:
    """`limit` caps prompt cost on every run, so it has to be a real cap and
    not a suggestion — and it must count tickets AFTER the current one is
    excluded, or asking for 2 can yield 1."""
    for index in range(6):
        adapter_harness.seed_ticket(subject=f"older {index}")
    current = adapter_harness.seed_ticket(subject="today's question")

    history = adapter_harness.port.fetch_requester_history(
        current.requester_email, exclude_ticket_id=current.ticket_id, limit=2
    )

    assert len(history) == 2
    assert [summary.subject for summary in history] == ["older 5", "older 4"]


def test_requester_history_is_empty_for_a_first_time_requester(
    adapter_harness: AdapterHarness,
) -> None:
    """No prior contact is a normal, common answer — not an error, and not
    a list containing the current ticket."""
    current = adapter_harness.seed_ticket(subject="today's question")

    history = adapter_harness.port.fetch_requester_history(
        current.requester_email, exclude_ticket_id=current.ticket_id
    )

    assert history == []


def test_requester_history_returns_normalized_summaries(
    adapter_harness: AdapterHarness,
) -> None:
    """Every field BUILD-PLAN §1.5 pins, normalized the same way for both
    adapters — no provider-shaped status string, no raw provider id."""
    adapter_harness.seed_ticket(subject="a previous one", tags=["vip"])
    current = adapter_harness.seed_ticket(subject="today's question")

    summary = adapter_harness.port.fetch_requester_history(
        current.requester_email, exclude_ticket_id=current.ticket_id
    )[0]

    assert isinstance(summary.id, str) and summary.id
    assert summary.subject == "a previous one"
    assert summary.status in ("new", "open", "pending", "solved")
    assert summary.created_at is not None
    assert "vip" in summary.tags
