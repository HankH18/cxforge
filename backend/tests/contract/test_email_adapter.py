"""EmailAdapter-specific contract tests.

Whether an outbound send genuinely goes through ``InMemoryEmailTransport``
(and an internal note genuinely never does) has no equivalent in
ZendeskAdapter's HTTP transport, so — like ``test_zendesk_adapter.py`` for
Zendesk's retry/backoff behavior — this is NOT part of the generic
parametrized suite in test_port_contract.py; it uses the email-only
``email_harness`` fixture directly. Still marked ``contract``: it verifies
HelpdeskPort semantics (what a caller can rely on) over the fake transport,
exactly as DESIGN's verification strategy describes this suite.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from ._fake_email import EmailHarness, make_email_harness

pytestmark = pytest.mark.contract


@pytest.fixture
def email_harness() -> Iterator[EmailHarness]:
    yield from make_email_harness()


def test_public_reply_is_recorded_by_the_fake_transport(email_harness: EmailHarness) -> None:
    seeded = email_harness.seed_ticket()

    ref = email_harness.port.post_public_reply(seeded.ticket_id, "<p>All set.</p>")

    assert len(email_harness.adapter.transport.sent) == 1
    sent = email_harness.adapter.transport.sent[0]
    assert sent.ticket_id == seeded.ticket_id
    assert sent.to == seeded.requester_email
    assert sent.html_body == "<p>All set.</p>"
    assert sent.message_id == ref.message_id


def test_internal_note_never_reaches_the_transport(email_harness: EmailHarness) -> None:
    """Plain email has no private-comment concept — an internal note must
    be recorded on the thread (readable via fetch_conversation) without
    ever being handed to the transport that would "send" it anywhere."""
    seeded = email_harness.seed_ticket()

    email_harness.port.post_internal_note(seeded.ticket_id, "Escalating per policy.")

    assert email_harness.adapter.transport.sent == []
    conversation = email_harness.port.fetch_conversation(seeded.ticket_id)
    assert len(conversation) == 1
    assert conversation[0].public is False


def test_writes_map_to_ai_author_kind(email_harness: EmailHarness) -> None:
    seeded = email_harness.seed_ticket()

    email_harness.port.post_public_reply(seeded.ticket_id, "<p>hi</p>")
    email_harness.port.post_internal_note(seeded.ticket_id, "note")

    conversation = email_harness.port.fetch_conversation(seeded.ticket_id)
    assert [message.author_kind for message in conversation] == ["ai", "ai"]


def test_fetch_ticket_on_unknown_thread_raises_typed_error(email_harness: EmailHarness) -> None:
    from helpdesk.errors import HelpdeskAPIError

    with pytest.raises(HelpdeskAPIError):
        email_harness.port.fetch_ticket("does-not-exist")
