"""ZendeskAdapter-specific contract tests.

Retry/backoff over HTTP and typed non-retryable-error surfacing are
inherently transport-level Zendesk behavior (a 429 with Retry-After, a 5xx,
a plain 4xx) with no equivalent in a transport-less adapter, so — unlike
test_port_contract.py — these are NOT part of the generic parametrized
suite; they use the Zendesk-only `zendesk_harness` fixture directly. Still
marked `contract`: they verify HelpdeskPort semantics (a call ultimately
succeeds, or raises the one typed error every caller is expected to handle)
over mocked Zendesk HTTP, exactly as DESIGN's verification strategy
describes this suite.

Every test here patches the adapter's sleep function (`zendesk_harness`
constructs the adapter with `sleep=sleeps.append`) — nothing in this file
ever really sleeps, so the whole suite runs in milliseconds even though it
exercises multi-attempt retry loops.
"""

from __future__ import annotations

import json

import httpx
import pytest

from helpdesk.errors import HelpdeskAPIError
from helpdesk.models import EscalationGroup

from ._fake_zendesk import BASE_URL, OAUTH_TOKEN
from .conftest import ZendeskHarness

pytestmark = pytest.mark.contract


def test_429_with_retry_after_is_retried_and_eventually_succeeds(
    zendesk_harness: ZendeskHarness,
) -> None:
    ticket_id = zendesk_harness.fake.seed_ticket()
    zendesk_harness.fake.queue_response(
        ticket_id,
        httpx.Response(429, headers={"Retry-After": "7"}, json={"error": "rate limited"}),
    )

    ref = zendesk_harness.port.post_public_reply(ticket_id, "<p>hi</p>")

    assert ref.public is True
    # The retry loop actually waited for the Retry-After value Zendesk sent
    # — not a fixed/default backoff — before the retry that succeeded.
    assert zendesk_harness.sleeps == [7.0]
    # Two PUTs: the one that got 429'd, and the retry that succeeded.
    assert zendesk_harness.fake.put_calls[ticket_id] == 2


def test_5xx_is_retried_with_backoff_and_eventually_succeeds(
    zendesk_harness: ZendeskHarness,
) -> None:
    ticket_id = zendesk_harness.fake.seed_ticket()
    zendesk_harness.fake.queue_response(ticket_id, httpx.Response(503, text="upstream unavailable"))

    ref = zendesk_harness.port.post_internal_note(ticket_id, "note")

    assert ref.public is False
    assert len(zendesk_harness.sleeps) == 1
    assert zendesk_harness.sleeps[0] > 0
    assert zendesk_harness.fake.put_calls[ticket_id] == 2


def test_non_retryable_4xx_surfaces_typed_error_without_retrying(
    zendesk_harness: ZendeskHarness,
) -> None:
    ticket_id = zendesk_harness.fake.seed_ticket()
    zendesk_harness.fake.queue_response(
        ticket_id,
        httpx.Response(422, json={"error": "RecordInvalid", "description": "bad body"}),
    )

    with pytest.raises(HelpdeskAPIError) as exc_info:
        zendesk_harness.port.post_public_reply(ticket_id, "<p>hi</p>")

    assert exc_info.value.status_code == 422
    assert zendesk_harness.sleeps == []
    # Exactly one attempt: a 422 is never retryable, so this must not have
    # silently retried (or, worse, silently succeeded) before raising.
    assert zendesk_harness.fake.put_calls[ticket_id] == 1


def test_retries_exhausted_on_repeated_429_surfaces_typed_error(
    zendesk_harness: ZendeskHarness,
) -> None:
    ticket_id = zendesk_harness.fake.seed_ticket()
    for _ in range(10):
        zendesk_harness.fake.queue_response(
            ticket_id,
            httpx.Response(429, headers={"Retry-After": "1"}, json={"error": "rate limited"}),
        )

    with pytest.raises(HelpdeskAPIError) as exc_info:
        zendesk_harness.port.post_public_reply(ticket_id, "<p>hi</p>")

    assert exc_info.value.status_code == 429


def test_public_reply_and_internal_note_are_two_separate_put_requests(
    zendesk_harness: ZendeskHarness,
) -> None:
    ticket_id = zendesk_harness.fake.seed_ticket()

    zendesk_harness.port.post_public_reply(ticket_id, "<p>hi</p>")
    zendesk_harness.port.post_internal_note(ticket_id, "note")

    assert zendesk_harness.fake.put_calls[ticket_id] == 2


def test_authorization_header_reaches_the_wire_on_read_and_write(
    zendesk_harness: ZendeskHarness,
) -> None:
    """Acceptance criterion 2 ("OAuth client, no API tokens") is otherwise
    verified only by reading zendesk_adapter.py:87 — nothing asserted that
    the ``Authorization: Bearer <token>`` header the adapter builds there
    actually reaches the transport. A regression that dropped or malformed
    it (e.g. a stray typo in the header name, or building the client before
    the token is set) would pass the whole suite silently. Inspecting
    ``zendesk_harness.fake.router.calls`` — respx's own record of every
    request it matched — proves the header was present on the real
    ``httpx.Request`` object, not just constructed somewhere and discarded.
    """
    ticket_id = zendesk_harness.fake.seed_ticket()

    zendesk_harness.port.fetch_ticket(ticket_id)  # read op
    zendesk_harness.port.post_public_reply(ticket_id, "<p>hi</p>")  # write op

    get_calls = [c for c in zendesk_harness.fake.router.calls if c.request.method == "GET"]
    put_calls = [c for c in zendesk_harness.fake.router.calls if c.request.method == "PUT"]
    assert get_calls, "expected the read op to issue a GET"
    assert put_calls, "expected the write op to issue a PUT"

    for call in get_calls + put_calls:
        assert call.request.headers["Authorization"] == f"Bearer {OAUTH_TOKEN}"


def test_fake_tags_field_replaces_the_full_tag_set(zendesk_harness: ZendeskHarness) -> None:
    """Real Zendesk's ``tags`` field on a ticket update REPLACES the entire
    tag set — unlike ``additional_tags``, which is purely additive. The fake
    must reproduce that destructive behavior faithfully: if it silently
    ignored ``tags`` instead, a test asserting the adapter never sends it
    (below) would be exercising a fake that couldn't tell the difference,
    and a real regression that started sending ``tags`` would only be
    caught in production — by an infinite webhook loop, once the
    ai-processed loop-guard tag got wiped off a live ticket.
    """
    ticket_id = zendesk_harness.fake.seed_ticket(tags=["existing-tag", "ai-processed"])

    # No adapter code path ever sends `tags` (see the next test) — this
    # simulates what a raw PUT carrying it would do, exactly as respx would
    # dispatch such a request to this same handler.
    request = httpx.Request(
        "PUT",
        f"{BASE_URL}/tickets/{ticket_id}.json",
        json={"ticket": {"tags": ["vip"]}},
    )
    zendesk_harness.fake._put_ticket(request, ticket_id)

    ticket = zendesk_harness.port.fetch_ticket(ticket_id)
    assert ticket.tags == ["vip"]
    assert "existing-tag" not in ticket.tags
    assert "ai-processed" not in ticket.tags


def test_adapter_writes_never_send_the_destructive_tags_field(
    zendesk_harness: ZendeskHarness,
) -> None:
    """Every write funnels through ``_update_ticket``, which must use only
    ``additional_tags`` (additive) — see the previous test for what the
    ``tags`` field (full replace) would do to a ticket's existing tags,
    including the ai-processed loop-guard, if it were ever sent instead."""
    ticket_id = zendesk_harness.fake.seed_ticket(tags=["existing-tag"])
    group = EscalationGroup(group_id="42", name="Escalations")

    zendesk_harness.port.post_public_reply(ticket_id, "<p>hi</p>")
    zendesk_harness.port.post_internal_note(ticket_id, "note")
    zendesk_harness.port.add_tags(ticket_id, ["vip"])
    zendesk_harness.port.set_status(ticket_id, "pending")
    zendesk_harness.port.assign_group(ticket_id, group)

    put_calls = [c for c in zendesk_harness.fake.router.calls if c.request.method == "PUT"]
    assert len(put_calls) == 5
    for call in put_calls:
        body = json.loads(call.request.content)["ticket"]
        assert "tags" not in body
