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
import logging

import httpx
import pytest

from helpdesk.errors import HelpdeskAPIError, HelpdeskConfigError
from helpdesk.models import EscalationGroup
from helpdesk.zendesk_adapter import ZendeskAdapter

from ._fake_zendesk import AI_USER_ID, BASE_URL, OAUTH_TOKEN, SUBDOMAIN
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
    tag set. The fake must reproduce that destructive behavior faithfully: if
    it silently ignored ``tags`` instead, a test asserting the adapter never
    sends it (below) would be exercising a fake that couldn't tell the
    difference, and a real regression that started sending ``tags`` would only
    be caught in production — by an infinite webhook loop, once the
    ai-processed loop-guard tag got wiped off a live ticket.

    MEASURED on the live account 2026-08-17: ``{"ticket": {"tags":
    ["replace-probe"]}}`` took ticket 3 from ``['probe-post-tags']`` to
    ``['replace-probe']``. Every assertion below therefore still describes real
    Zendesk exactly.

    The docstring formerly added "unlike ``additional_tags``, which is purely
    additive". That clause was false — see
    ``test_additional_tags_is_inert_on_a_ticket_update`` — and it is the belief
    the whole defect rested on, so it is removed rather than softened.
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
    """No ticket update may carry EITHER tag field.

    ``tags`` would replace the ticket's whole set (previous test) including the
    ai-processed loop guard. ``additional_tags`` is worse in a quieter way: it
    is silently discarded, so it looks like a successful additive write and is
    not one — that is the 2026-08-17 defect verbatim.

    The old form counted every PUT and asserted ``== 5``, which was the same
    statement when the ticket update was the only write the adapter knew how to
    make. It is replaced by the full ordered request sequence — strictly more
    information than a count, and it pins the two things a count cannot: that
    each operation goes to the endpoint that can actually perform it, and that
    the loop-guard tag precedes the update it guards.

    ``add_tags`` no longer appears as a ticket update at all, and that is the
    fix rather than a relaxation: a ticket update is incapable of adding a tag
    without replacing the set.
    """
    ticket_id = zendesk_harness.fake.seed_ticket(tags=["existing-tag"])
    group = EscalationGroup(group_id="42", name="Escalations")

    zendesk_harness.port.post_public_reply(ticket_id, "<p>hi</p>")
    zendesk_harness.port.post_internal_note(ticket_id, "note")
    zendesk_harness.port.add_tags(ticket_id, ["vip"])
    zendesk_harness.port.set_status(ticket_id, "pending")
    zendesk_harness.port.assign_group(ticket_id, group)

    writes = [
        (c.request.method, c.request.url.path)
        for c in zendesk_harness.fake.router.calls
        if c.request.method in {"PUT", "POST", "DELETE"}
    ]
    ticket_path = f"/api/v2/tickets/{ticket_id}.json"
    tags_path = f"/api/v2/tickets/{ticket_id}/tags.json"
    assert writes == [
        ("PUT", tags_path),      # loop guard, BEFORE the reply it guards
        ("PUT", ticket_path),    # post_public_reply
        ("PUT", ticket_path),    # post_internal_note — guard already applied
        ("PUT", tags_path),      # add_tags: additive endpoint, not a ticket update
        ("PUT", ticket_path),    # set_status
        ("PUT", ticket_path),    # assign_group
    ], writes

    ticket_puts = [
        c
        for c in zendesk_harness.fake.router.calls
        if c.request.method == "PUT" and c.request.url.path == ticket_path
    ]
    assert len(ticket_puts) == 4
    for call in ticket_puts:
        body = json.loads(call.request.content)["ticket"]
        assert "tags" not in body
        assert "additional_tags" not in body

    # Every additive write carries the loop guard, and must be a PUT to the
    # sub-resource — never a POST, which replaces (measured).
    tag_calls = [
        c for c in zendesk_harness.fake.router.calls if c.request.url.path == tags_path
    ]
    assert len(tag_calls) == 2
    for call in tag_calls:
        assert call.request.method == "PUT", "POST /tags.json REPLACES the tag set"
        assert "ai-processed" in json.loads(call.request.content)["tags"]

    # And the end state is what the whole thing is for.
    ticket = zendesk_harness.port.fetch_ticket(ticket_id)
    assert set(ticket.tags) == {"existing-tag", "ai-processed", "vip"}


def test_additional_tags_is_inert_on_a_ticket_update(
    zendesk_harness: ZendeskHarness,
) -> None:
    """The measurement the adapter was built on the opposite of.

    ``additional_tags`` is an ``update_many`` field. On a single-ticket update
    Zendesk answers 200 and discards it, exactly as it discards a field that
    does not exist at all. Both are asserted here, together, because it is the
    *pair* that establishes the mechanism: the field is not broken, it is simply
    not in this endpoint's schema and unknown keys are dropped silently.

    Live evidence, ticket 3 on 2026-08-17, tags ``['cxforge-verify']``
    throughout::

        PUT /tickets/3.json {"ticket":{"additional_tags":["probe-alone","ai-processed"]}} -> 200
        GET /tickets/3.json -> tags ['cxforge-verify']
        PUT /tickets/3.json {"ticket":{"additional_tags":["probe-mixed"],"status":"open"}} -> 200
        GET /tickets/3.json -> tags ['cxforge-verify']
        PUT /tickets/3.json {"ticket":{"banana_tags":["nonsense"],"status":"open"}} -> 200
        GET /tickets/3.json -> tags ['cxforge-verify']
    """
    ticket_id = zendesk_harness.fake.seed_ticket(tags=["existing-tag"])

    for field in ("additional_tags", "banana_tags"):
        request = httpx.Request(
            "PUT",
            f"{BASE_URL}/tickets/{ticket_id}.json",
            json={"ticket": {field: ["should-be-discarded"], "status": "open"}},
        )
        response = zendesk_harness.fake._put_ticket(request, ticket_id)

        assert response.status_code == 200, f"{field} must not be an error, it must be ignored"
        assert zendesk_harness.port.fetch_ticket(ticket_id).tags == ["existing-tag"]
        assert zendesk_harness.port.fetch_ticket(ticket_id).status == "open", (
            "the rest of the patch must still apply — the field is dropped, not the request"
        )


def test_post_to_the_tags_subresource_replaces_the_whole_set(
    zendesk_harness: ZendeskHarness,
) -> None:
    """``POST /tickets/{id}/tags.json`` is destructive despite reading as "add".

    Measured 2026-08-17: a POST of ``["probe-post-tags"]`` took ticket 3 from
    ``['ai-processed', 'cxforge-verify', 'probe-tags-endpoint']`` to
    ``['probe-post-tags']`` — it wiped the loop guard. The fake reproduces that
    so a future "simplification" of ``_merge_tags`` from PUT to POST fails here
    instead of silently disarming the guard in production.
    """
    ticket_id = zendesk_harness.fake.seed_ticket(tags=["existing-tag", "ai-processed"])

    request = httpx.Request(
        "POST", f"{BASE_URL}/tickets/{ticket_id}/tags.json", json={"tags": ["only-this"]}
    )
    response = zendesk_harness.fake._post_tags(request, ticket_id)

    assert response.status_code == 201
    assert zendesk_harness.port.fetch_ticket(ticket_id).tags == ["only-this"]
    assert "ai-processed" not in zendesk_harness.port.fetch_ticket(ticket_id).tags


def test_loop_guard_tag_is_applied_before_the_write_it_guards(
    zendesk_harness: ZendeskHarness,
) -> None:
    """Ordering, not just presence.

    Zendesk evaluates a trigger's conditions against the state the update
    produces, so the reply and the tag cannot be reordered: a public reply that
    lands before ``ai-processed`` exists satisfies the trigger's "Comment is
    Public" with its nullifying condition unmet, and the webhook fires on the
    agent's own comment. Tagging after the reply guards every future reply and
    not the one that just went out.
    """
    ticket_id = zendesk_harness.fake.seed_ticket()

    zendesk_harness.port.post_public_reply(ticket_id, "<p>hi</p>")

    paths = [
        (c.request.method, c.request.url.path)
        for c in zendesk_harness.fake.router.calls
        if c.request.method in {"PUT", "POST"}
    ]
    assert paths == [
        ("PUT", f"/api/v2/tickets/{ticket_id}/tags.json"),
        ("PUT", f"/api/v2/tickets/{ticket_id}.json"),
    ], f"the tag write must precede the ticket update, got {paths}"


def test_a_failed_loop_guard_tag_write_aborts_the_reply(
    zendesk_harness: ZendeskHarness,
) -> None:
    """If the guard cannot be applied, nothing may be posted.

    An unguarded public reply is how the loop starts, and a loop spams a real
    customer and bills real tokens until a human notices. A failed run is the
    cheaper mistake, so the tag failure must propagate and no comment may
    exist afterwards.
    """
    ticket_id = zendesk_harness.fake.seed_ticket()
    zendesk_harness.fake.queue_tag_response(
        ticket_id, httpx.Response(422, json={"error": "RecordInvalid"})
    )

    with pytest.raises(HelpdeskAPIError) as exc_info:
        zendesk_harness.port.post_public_reply(ticket_id, "<p>hi</p>")

    assert exc_info.value.status_code == 422
    assert zendesk_harness.fake.put_calls.get(ticket_id, 0) == 0, (
        "the ticket update must not have been attempted at all"
    )
    assert zendesk_harness.port.fetch_conversation(ticket_id) == []


def test_a_ticket_patch_carrying_a_tag_field_is_refused(
    zendesk_harness: ZendeskHarness,
) -> None:
    """The structural guard against reintroducing the defect.

    ``_update_ticket`` is the funnel every write passes through. Funnelling was
    never the problem — the funnel wrote the tag into a field Zendesk ignores.
    So the funnel now refuses to carry tags at all, rather than trusting the
    next author to remember which of two similarly-named fields is inert.
    """
    ticket_id = zendesk_harness.fake.seed_ticket()
    adapter = zendesk_harness.port
    assert isinstance(adapter, ZendeskAdapter)

    for field in ("tags", "additional_tags"):
        with pytest.raises(HelpdeskConfigError, match=field):
            adapter._update_ticket(ticket_id, {field: ["nope"]})

    assert zendesk_harness.fake.put_calls.get(ticket_id, 0) == 0


# ---------------------------------------------------------------------------
# ZENDESK_AI_USER_ID vs. the identity the token actually acts as (defect 2).
#
# The configured id and the token's real identity were two independent facts
# and nothing compared them. On 2026-08-17 they disagreed in production:
# ZENDESK_AI_USER_ID named the dedicated "Othram AI Agent" (54404962250395)
# while the token acted as the owner's admin account (54402664002843), which
# is who authored the AI's reply. Ingress's self-event guard compares
# comment_author_id against the configured value, so it was comparing against
# an id that appears in no event this system can ever receive — and every test
# passed, because the simulator hardcoded the comment author to the configured
# id and made the assumption true by construction.
# ---------------------------------------------------------------------------

OTHER_USER_ID = "54402664002843"  # shape of the real owner-admin id


def test_verify_ai_user_id_accepts_a_matching_configuration(
    zendesk_harness: ZendeskHarness,
) -> None:
    adapter = zendesk_harness.port
    assert isinstance(adapter, ZendeskAdapter)

    assert adapter.verify_ai_user_id() == AI_USER_ID


def test_verify_ai_user_id_rejects_the_production_mismatch(
    zendesk_harness: ZendeskHarness,
) -> None:
    """The exact 2026-08-17 state: config names one user, the token is another."""
    zendesk_harness.fake.authenticated_user_id = OTHER_USER_ID
    adapter = zendesk_harness.port
    assert isinstance(adapter, ZendeskAdapter)

    with pytest.raises(HelpdeskConfigError) as exc_info:
        adapter.verify_ai_user_id()

    message = str(exc_info.value)
    assert AI_USER_ID in message, "must name the configured id"
    assert OTHER_USER_ID in message, "must name the id the token actually is"
    # The remedy has to be in the message: this failure was misread for an hour
    # because nothing said which of the two ids was the real one.
    assert f"ZENDESK_AI_USER_ID={OTHER_USER_ID}" in message


def test_verify_ai_user_id_warns_rather_than_fails_when_unset(
    zendesk_harness: ZendeskHarness, caplog: pytest.LogCaptureFixture
) -> None:
    """Blank is "guard off", not "guard wrong" — .env.example ships it blank."""
    adapter = ZendeskAdapter(
        subdomain=SUBDOMAIN, oauth_token=OAUTH_TOKEN, ai_user_id="", sleep=lambda _: None
    )

    with caplog.at_level(logging.WARNING):
        assert adapter.verify_ai_user_id() == AI_USER_ID

    assert "ZENDESK_AI_USER_ID is not set" in caplog.text
    assert AI_USER_ID in caplog.text


def test_a_reply_authored_by_an_unexpected_user_is_reported_at_error(
    zendesk_harness: ZendeskHarness, caplog: pytest.LogCaptureFixture
) -> None:
    """The zero-cost detector: read the author back off the write response.

    This is the one that would have caught the live defect at the moment it
    happened, with no extra request — the audit Zendesk returns from the ticket
    update already names the author of the comment it just created. It compares
    configuration against the effect the system produced, rather than against
    more configuration.
    """
    zendesk_harness.fake.authenticated_user_id = OTHER_USER_ID
    ticket_id = zendesk_harness.fake.seed_ticket()

    with caplog.at_level(logging.ERROR):
        zendesk_harness.port.post_public_reply(ticket_id, "<p>hi</p>")

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "a reply authored by an unexpected user must be an ERROR"
    assert OTHER_USER_ID in caplog.text
    assert AI_USER_ID in caplog.text
    assert "loop" in caplog.text.lower()


def test_the_detector_does_not_fire_when_the_author_matches(
    zendesk_harness: ZendeskHarness, caplog: pytest.LogCaptureFixture
) -> None:
    """The control. Without this, an unconditional ERROR would pass the test
    above while telling operators nothing."""
    ticket_id = zendesk_harness.fake.seed_ticket()

    with caplog.at_level(logging.ERROR):
        zendesk_harness.port.post_public_reply(ticket_id, "<p>hi</p>")
        zendesk_harness.port.post_internal_note(ticket_id, "note")

    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_a_mismatched_reply_is_still_posted(zendesk_harness: ZendeskHarness) -> None:
    """The detector must not raise.

    The comment is already visible to the customer by the time the author is
    known. Raising would send `worker.main.run_ticket` down its failure path,
    release the dedup row (ADR-003), and make the next delivery of the same
    event post a SECOND reply to a real customer. A degraded loop-guard line is
    the lesser harm, and `verify_ai_user_id` is the raising form for a
    preflight where nothing has been written yet.
    """
    zendesk_harness.fake.authenticated_user_id = OTHER_USER_ID
    ticket_id = zendesk_harness.fake.seed_ticket()

    ref = zendesk_harness.port.post_public_reply(ticket_id, "<p>hi</p>")

    assert ref.public is True
    assert len(zendesk_harness.port.fetch_conversation(ticket_id)) == 1


def test_the_authenticated_identity_is_resolved_once_and_cached(
    zendesk_harness: ZendeskHarness,
) -> None:
    """One request per adapter, not one per call: a token's identity cannot
    change without a new token, and a refresh preserves it."""
    adapter = zendesk_harness.port
    assert isinstance(adapter, ZendeskAdapter)

    adapter.authenticated_user_id()
    adapter.authenticated_user_id()
    adapter.verify_ai_user_id()

    me_calls = [
        c
        for c in zendesk_harness.fake.router.calls
        if c.request.url.path.endswith("/users/me.json")
    ]
    assert len(me_calls) == 1
